"""What is playing: the service, its programme, and the signal carrying it.

Three publishers share one hook. `session.nav.event` is a plain list of callables
that enigma2 calls with an event identifier for everything that happens to the
playing service, and each publisher attaches its own listener to it so that one
of them failing to bind does not cost the other two their capability.

The event identifiers are read off `iPlayableService` by name and never
hard-coded. Their *values* are an enumeration in a C++ header that has grown in
the middle more than once; their *names* have been stable for a decade.

Two things here are not events at all, and both are deliberate.

The programme changes without anything happening on the box: at some point the
news ends and the weather begins, and no zap, no tune and no EPG update need
occur for the „now" programme to be a different one. So `epg` re-reads itself
when the programme it published was due to end.

The signal is a measurement, not a state. It drifts with the weather and is
worth having on a slow tick rather than on an event that will not come.
"""

import time

from .enigma2 import (
    Ticker,
    constant,
    current_service,
    current_service_reference,
    identity,
    missing,
    navigation,
    reference_string,
    same_service,
    service_name,
    service_reference,
)
from .log import get_logger
from .publisher import Publisher

LOG = get_logger("service")

# How long a zap has to show up on the `service` topic before `cmd/zap` is
# called a failure. Generous: a satellite tune across a band takes a while.
ZAP_VERIFY_MILLISECONDS = 5000

TUNER_POLL_MILLISECONDS = 60000

# enigma2 reports signal quality and power as a 16-bit number.
SIGNAL_SCALE = 65535

# The programme boundary is re-checked at least this often even when the current
# programme runs for hours, so a cleared EPG cache is noticed.
EPG_MAX_SLEEP_MILLISECONDS = 15 * 60 * 1000
EPG_MIN_SLEEP_MILLISECONDS = 5000

# What „nothing is playing" and „nothing is tuned" look like. The keys are all
# there with null values rather than the topic being absent, because the
# contract promises that a field which has no value right now is `null` and that
# a key is never silently dropped.
EMPTY_SERVICE = {"sref": None, "name": None, "bouquet": None, "provider": None,
                 "width": None, "height": None}
EMPTY_TUNER = {"snr": None, "agc": None, "ber": None, "tuner": None}


def event_id(name):
    """`iPlayableService.<name>`, or None on an image that lacks it."""
    return constant("iPlayableService", name)


def info_constant(name):
    return constant("iServiceInformation", name)


def frontend_constant(name):
    return constant("iFrontendInformation", name)


# ------------------------------------------------------------------- reading --


def _service_info(session):
    service = current_service(session)
    if service is None:
        return None
    getter = getattr(service, "info", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        LOG.debug("the playing service has no info")
        return None


def _text(info, name):
    key = info_constant(name)
    if info is None or key is None:
        return None
    try:
        value = info.getInfoString(key)
    except Exception:
        return None
    value = str(value or "").strip()
    return value or None


def _number(info, name):
    """An integer field, with enigma2's „I do not know yet" spelled as None.

    Video width and height are `-1` (and on some images `0`) between the zap and
    the first decoded frame. Publishing that as a resolution would be a lie a
    consumer cannot tell from a real one.
    """
    key = info_constant(name)
    if info is None or key is None:
        return None
    try:
        value = int(info.getInfo(key))
    except Exception:
        return None
    return value if value > 0 else None


def read_service(session, bouquet_lookup=None):
    """The `service` payload, or None when nothing is playing."""
    sref = current_service_reference(session)
    if not sref:
        return None
    info = _service_info(session)
    name = service_name(sref)
    if not name and info is not None:
        try:
            name = str(info.getName() or "")
        except Exception:
            name = ""
    bouquet = None
    if bouquet_lookup is not None:
        try:
            bouquet = bouquet_lookup(sref)
        except Exception:
            LOG.debug("could not work out which bouquet %s came from", sref)
    return {
        "sref": sref,
        "name": name or None,
        "bouquet": bouquet,
        "provider": _text(info, "sProvider"),
        "width": _number(info, "sVideoWidth"),
        "height": _number(info, "sVideoHeight"),
    }


def _event_payload(event):
    """One `eServiceEvent` as the contract spells it."""
    if event is None:
        return None
    try:
        begin = int(event.getBeginTime() or 0)
        duration = int(event.getDuration() or 0)
        return {
            "title": str(event.getEventName() or ""),
            "begin": begin,
            "end": begin + duration,
            "event_id": int(event.getEventId() or 0),
            "short": str(event.getShortDescription() or ""),
            "long": str(event.getExtendedDescription() or ""),
        }
    except Exception:
        LOG.debug("an EPG event could not be read")
        return None


def read_epg(session):
    """The `epg` payload - `now` and `next`, either of which may be null."""
    info = _service_info(session)
    if info is None:
        return {"now": None, "next": None}
    payload = {}
    for key, index in (("now", 0), ("next", 1)):
        try:
            event = info.getEvent(index)
        except Exception:
            event = None
        payload[key] = _event_payload(event)
    return payload


def _tuner_letter(number):
    """Frontend 0 is tuner A, which is how every receiver's own menus spell it."""
    try:
        index = int(number)
    except (TypeError, ValueError):
        return None
    if index < 0 or index > 25:
        return None
    return chr(ord("A") + index)


def _percent(value):
    if value is None:
        return None
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return None
    if raw < 0:
        return None
    return int(round(raw * 100.0 / SIGNAL_SCALE))


def _frontend(session):
    """The tuner behind what is playing. None for a stream, a file, or standby."""
    service = current_service(session)
    if service is None:
        return None
    getter = getattr(service, "frontendInfo", None)
    if getter is None:
        return None
    try:
        # Returns None for anything not coming off a tuner - IPTV, a recording
        # being played back - which is not a failure and not worth a log line.
        return getter()
    except Exception:
        return None


def _status_dict(frontend):
    getter = getattr(frontend, "getFrontendStatus", None)
    if getter is None:
        return {}
    try:
        return getter() or {}
    except Exception:
        return {}


def read_tuner(session):
    """The `tuner` payload, or None when nothing is tuned.

    The status dictionary is read first because it is what enigma2 itself fills
    in one pass, and `getFrontendInfo` is the per-value fallback for an image
    that does not offer the dictionary. 🔴 Every key is read with `.get`: the
    signal-quality-in-decibels key is *absent* rather than negative when the
    driver has no reading, and one `KeyError` here would cost the whole topic.
    """
    frontend = _frontend(session)
    if frontend is None:
        return None

    status = _status_dict(frontend)

    def measure(key, constant_name):
        if key in status:
            return status.get(key)
        enum = frontend_constant(constant_name)
        if enum is None:
            return None
        try:
            return frontend.getFrontendInfo(enum)
        except Exception:
            return None

    quality = measure("tuner_signal_quality", "signalQuality")
    power = measure("tuner_signal_power", "signalPower")
    errors = measure("tuner_bit_error_rate", "bitErrorRate")

    tuner = None
    data_getter = getattr(frontend, "getFrontendData", None)
    if data_getter is not None:
        try:
            data = data_getter() or {}
            tuner = _tuner_letter(data.get("tuner_number"))
        except Exception:
            tuner = None

    if quality is None and power is None and errors is None and tuner is None:
        return None
    try:
        # Raw, not a percentage. enigma2's own web interface scales the error
        # rate by 65535 like the other two, which produces a number that looks
        # like a percentage and means nothing.
        ber = None if errors is None else int(errors)
    except (TypeError, ValueError):
        ber = None
    return {"snr": _percent(quality), "agc": _percent(power), "ber": ber, "tuner": tuner}


# -------------------------------------------------------------------- zapping --
#
# A zap from outside goes the way the remote's number entry goes, so that it is
# in the receiver's own zap history - the list KEY_NEXT and KEY_PREVIOUS open -
# exactly as a zap made with the remote is (ADR-0014). The image only records a
# zap that passes through its channel selection: `ChannelSelection.zap` and its
# timeshift callback call `addToHistory`, and `Navigation.playService` never
# does. So the plugin does not reimplement the number zap; it calls the image's
# own `InfoBar.instance.selectAndStartService(service, bouquet)`, which enters
# the bouquet, selects the service, zaps and corrects the channel number.
#
# Three situations keep the old `playService`, unrecorded, because the recorded
# path would do something worse than not being recorded:
#
# - **timeshift**: `ChannelSelection.zap` asks `checkTimeshiftRunning` first,
#   and during timeshift that opens a question on the television with no
#   timeout. A zap from a phone must not leave a dialogue on somebody's screen;
# - **picture-in-picture zap mode** (`servicelist.dopipzap`): the channel list
#   would zap the small picture, and the command means the main one;
# - **a channel in no published bouquet** (a radio service, a bouquet
#   `bouquets_for_select` leaves out, a reference in no bouquet at all): there
#   is no bouquet to enter it through.
#
# And one this plugin adds for the same reason `cmd/bouquet` refuses it: a
# channel list in radio mode. Entering a television bouquet under the radio
# root would build a path the receiver then saves as its radio list's root.

# How long a zap waiting for the receiver to leave standby waits for the
# standby screen to close before it says so on `last_error`.
WAKE_WAIT_MILLISECONDS = 5000

WAKE_TIMEOUT = "the receiver did not leave standby"
UNWATCHABLE = (
    "this image's standby screen cannot be watched, so a zap would be undone by the wake"
)

# Why a zap was played directly: the one fallback logged once per reference.
NO_BOUQUET = "it is in no published bouquet"

# References already logged as unrecordable, so a household zapping the same
# radio station every evening does not log the same line every evening.
_unrecorded_noted = set()


def forget_unrecorded():
    """Test seam: the set above is module state and outlives a test."""
    _unrecorded_noted.clear()


def infobar_instance():
    try:
        from Screens.InfoBar import InfoBar
    except Exception:
        return None
    return getattr(InfoBar, "instance", None)


def timeshift_active(infobar):
    """Whether `checkTimeshiftRunning` would ask its question now.

    The image's own test, read out of `Components/Timeshift.py`: the service is
    seekable and timeshift is enabled, or a timeshift is waiting to be saved.
    An image with no timeshift at all has none of these names and cannot be in
    timeshift. One that has them and raises is treated as in timeshift: the
    caller then takes the path that cannot open a question.
    """
    if infobar is None:
        return False
    try:
        if getattr(infobar, "save_current_timeshift", False):
            return True
        seekable = getattr(infobar, "isSeekable", None)
        enabled = getattr(infobar, "timeshiftEnabled", None)
        if not callable(seekable) or not callable(enabled):
            return False
        return bool(seekable()) and bool(enabled())
    except Exception:
        LOG.debug("could not tell whether timeshift is running; assuming it is")
        return True


def _tv_mode(servicelist):
    try:
        from Screens.ChannelSelection import ChannelSelection

        expected = getattr(ChannelSelection, "MODE_TV", 0)
    except Exception:
        expected = 0
    return getattr(servicelist, "mode", expected) == expected


def _member(bouquet, wanted):
    """The bouquet's own spelling of the service with identity `wanted`, or None."""
    for channel in bouquet.get("channels") or ():
        if identity(channel.get("sref")) == wanted:
            return channel.get("sref")
    return None


def zap_bouquet(servicelist, channels, sref):
    """`(bouquet, service)` for a zap to `sref`, as reference objects, or None.

    The bouquet, in order: the one the channel list is on now, when it holds
    the service - so a zap from the channel select never moves the channel
    list's context - and otherwise the first published bouquet that holds it,
    which is the one the service is announced under. Membership is by
    identity, against the `channels` cache the plugin already keeps, and the
    service comes back in that bouquet's own spelling, which is the one the
    channel list finds when it is asked to select it.
    """
    bouquets = getattr(channels, "bouquets", None) or []
    wanted = identity(sref)
    if not wanted:
        return None
    root = servicelist.getRoot()
    root_string = reference_string(root)
    if root is not None and root_string:
        for bouquet in bouquets:
            if reference_string(bouquet.get("sref")) == root_string:
                member = _member(bouquet, wanted)
                if member:
                    return root, service_reference(member)
    for bouquet in bouquets:
        member = _member(bouquet, wanted)
        if member:
            return service_reference(bouquet.get("sref")), service_reference(member)
    return None


def _playing(session):
    return current_service_reference(session)


def _recorded_zap(session, sref, channels):
    """Zap through the image's number-zap path. `(True, None)`, or `(False, why)`.

    A False is never a refusal: the caller plays the service the old way, and
    `why` is what the log says about it.
    """
    infobar = infobar_instance()
    servicelist = getattr(infobar, "servicelist", None) if infobar is not None else None
    start = getattr(infobar, "selectAndStartService", None) if infobar is not None else None
    if servicelist is None or not callable(start) or not callable(
        getattr(servicelist, "getRoot", None)
    ):
        return False, "this image has no channel-list zap to record it with"
    if getattr(servicelist, "dopipzap", False):
        return False, "the channel list is in picture-in-picture zap mode"
    if timeshift_active(infobar):
        return False, "timeshift is active"
    if not _tv_mode(servicelist):
        return False, "the channel list is not in television mode"
    try:
        chosen = zap_bouquet(servicelist, channels, sref)
    except Exception:
        LOG.exception("could not choose a bouquet to zap through")
        return False, "the channel list could not be read"
    if chosen is None or chosen[0] is None or chosen[1] is None:
        return False, NO_BOUQUET
    bouquet, service = chosen

    before = _playing(session)
    try:
        start(service, bouquet)
    except Exception:
        LOG.exception("selectAndStartService raised")
        return False, "the channel list's zap raised"
    after = _playing(session)
    if same_service(after, sref) or same_service(after, before) or not after:
        # Tuned - or not yet, which is what a parental-control PIN on the
        # television looks like. Either way `service.expect` has the last word.
        return True, None
    # 🔴 `selectAndStartService` zaps whatever it managed to select, and a
    # bouquet file edited since the `channels` cache was read can leave the
    # selection on a neighbour. The wrong channel on the television is worse
    # than an unrecorded zap to the right one.
    LOG.warning("the channel list tuned %s instead of %s; playing it directly", after, sref)
    return False, "the channel list tuned another service"


def _note_unrecorded(sref, why):
    key = identity(sref)
    if key in _unrecorded_noted:
        return
    _unrecorded_noted.add(key)
    LOG.info("zapping to %s without the channel list, so it is not in the zap history: %s",
             sref, why)


def _zap_awake(session, sref, channels=None, on_zap=None):
    """The zap itself, on a receiver that is awake. None, or the refusal."""
    nav = navigation(session)
    player = getattr(nav, "playService", None) if nav is not None else None
    if player is None:
        return "this image's navigation has no playService"
    reference = service_reference(sref)
    if reference is None:
        return "'" + str(sref) + "' is not a service reference"
    recorded, why = _recorded_zap(session, sref, channels)
    if not recorded:
        if why == NO_BOUQUET:
            _note_unrecorded(sref, why)
        else:
            LOG.info("zapping to %s without the channel list: %s", sref, why)
        try:
            player(reference)
        except Exception as error:
            LOG.exception("playService raised")
            return type(error).__name__ + ": " + str(error)
    if on_zap is not None:
        on_zap(sref)
    return None


# The one zap waiting for the standby screen to close. A second request while
# the first still waits replaces it: the household pressed twice, and the later
# press is the one they meant.
_pending_wake = None


class _AfterWake:
    """Run one action once the receiver has really left standby.

    `inStandby.Power()` closes the standby screen, and enigma2 closes a screen
    on the **next** turn of the main loop (`Session.close` starts a 0 ms timer).
    The standby screen's own `__onClose` then plays the service the box slept
    on. A zap made in the same turn as the wake is overwritten by that restore.

    So the plugin appends to the screen's `onClose` *before* waking it. The
    screen registered its own `__onClose` in its constructor, so this one runs
    after the restore, and it starts a 0 ms single-shot timer: the zap runs on
    the turn after that, as a zap from the remote would. The restore itself is
    a `playService` and stays out of the history - it is the channel the
    receiver was already on.
    """

    def __init__(self, command, action, report=None, allowed=None):
        self.command = command
        self.action = action
        self.report = report
        self.allowed = allowed
        self.screen = None
        self.closed = False
        self.finished = False
        self._run = Ticker(self._fire, "zap after wake")
        self._timeout = Ticker(self._timed_out, "wake wait")

    def begin(self, screen):
        """Hook the screen and wake it. None, or why it could not be done."""
        from .power import wake

        hook = getattr(screen, "onClose", None)
        if hook is None:
            return UNWATCHABLE
        try:
            hook.append(self._closed)
        except Exception as error:
            LOG.exception("could not watch the standby screen")
            return type(error).__name__ + ": " + str(error)
        self.screen = screen
        self._timeout.start(WAKE_WAIT_MILLISECONDS, True)
        error = wake()
        if error:
            self.cancel()
            return error
        return None

    def cancel(self):
        self.finished = True
        self._run.stop()
        self._timeout.stop()
        screen, self.screen = self.screen, None
        if screen is not None and not self.closed:
            # Only while the screen is not closing: enigma2 walks `onClose`
            # while it calls it, and a removal then skips the next listener.
            try:
                if self._closed in screen.onClose:
                    screen.onClose.remove(self._closed)
            except Exception:
                LOG.debug("could not stop watching the standby screen")

    def _closed(self):
        # Called by enigma2 while it closes the standby screen; nothing here may
        # raise into that loop, and nothing here removes itself from it.
        try:
            self.closed = True
            if self.finished:
                return
            self._timeout.stop()
            self._run.start(0, True)
        except Exception:
            LOG.exception("could not schedule the zap after the wake")

    def _fire(self):
        global _pending_wake
        if self.finished:
            return
        self.finished = True
        if _pending_wake is self:
            _pending_wake = None
        if self.allowed is not None and not self.allowed():
            LOG.info("dropping the %s that waited for the wake: the plugin is going away",
                     self.command)
            return
        error = self.action()
        if error and self.report is not None:
            self.report(self.command, error)

    def _timed_out(self):
        global _pending_wake
        if self.finished:
            return
        self.cancel()
        if _pending_wake is self:
            _pending_wake = None
        if self.allowed is not None and not self.allowed():
            return
        if self.report is not None:
            self.report(self.command, WAKE_TIMEOUT)


def run_after_wake(command, action, report=None, allowed=None):
    """Wake the receiver, and run `action` once its own restore is done.

    None when the action ran or is waiting for the wake; otherwise why not. A
    refusal from the action once it runs, and a wake that never finishes, go to
    `report(command, sentence)`. `allowed()` is asked again just before the
    action runs, so that a removal of the plugin accepted in between is not
    followed by a zap.
    """
    global _pending_wake
    from .power import standby_module

    module = standby_module()
    screen = getattr(module, "inStandby", None) if module is not None else None
    if screen is None:
        # Awake already - between the caller's look and this one.
        return action()
    if _pending_wake is not None:
        _pending_wake.cancel()
        _pending_wake = None
    waiter = _AfterWake(command, action, report, allowed)
    error = waiter.begin(screen)
    if error:
        return error
    _pending_wake = waiter
    return None


def zap(session, sref, channels=None, on_zap=None, report=None, allowed=None):
    """Tune to a service. None on success, otherwise the refusal.

    `channels` is the `channels` publisher, whose cache chooses the bouquet the
    zap goes through; without it every zap is played directly. `on_zap(sref)`
    is called when the zap has actually been made - at once, or after the wake
    - which is when its verification should start.

    From standby the receiver is woken first and the zap follows its restore
    (`run_after_wake`), so it is recorded like any other.
    """
    nav = navigation(session)
    if nav is None:
        return "there is no session to zap with"
    if getattr(nav, "playService", None) is None:
        return "this image's navigation has no playService"
    if service_reference(sref) is None:
        return "'" + str(sref) + "' is not a service reference"

    from .power import in_standby

    def awake():
        return _zap_awake(session, sref, channels, on_zap)

    if in_standby():
        return run_after_wake("zap", awake, report, allowed)
    return awake()


# ----------------------------------------------------------------- publishers --


class NavPublisher(Publisher):
    """A publisher that listens to `session.nav.event`."""

    # The `iPlayableService` names this publisher acts on.
    events = ()

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._wanted = set()
        self._nav = None

    def start(self):
        nav = navigation(self.session)
        if nav is None:
            missing("session.nav")
            return False
        hook = getattr(nav, "event", None)
        if hook is None:
            missing("session.nav.event")
            return False
        wanted = set()
        for name in self.events:
            value = event_id(name)
            if value is not None:
                wanted.add(value)
        if not wanted:
            return False
        try:
            hook.append(self._on_event)
        except Exception as error:
            missing("session.nav.event", error)
            return False
        self._wanted = wanted
        self._nav = nav
        return True

    def stop(self):
        nav, self._nav = self._nav, None
        if nav is None:
            return
        try:
            hook = getattr(nav, "event", None)
            if hook is not None and self._on_event in hook:
                hook.remove(self._on_event)
        except Exception:
            LOG.debug("could not detach from session.nav.event")

    def _on_event(self, event):
        # Called by enigma2 on the main thread for every service event there is,
        # including the ones this publisher does not care about.
        if event not in self._wanted:
            return
        try:
            self.on_service_event(event)
        except Exception:
            LOG.exception("the %s publisher raised on a service event", self.name)

    def on_service_event(self, event):
        raise NotImplementedError


class ServicePublisher(NavPublisher):
    """`service` - what is tuned, and what it looks like."""

    name = "service"
    events = ("evStart", "evUpdatedInfo", "evNewProgramInfo", "evTunedIn", "evEnd")

    def __init__(self, bridge=None):
        NavPublisher.__init__(self, bridge)
        self._current = None
        self._waiting_for = None
        self._verify = Ticker(self._zap_timed_out, "zap verify")

    # The resolution arrives after the first frame is decoded, so `evStart`
    # publishes a service with `width: null` and `evUpdatedInfo` publishes it
    # again with the numbers. Both are correct at the moment they are sent.
    def on_service_event(self, _event):
        self.refresh()

    def _bouquet_lookup(self):
        if self.bridge is None:
            return None
        channels = self.bridge.publisher("channels")
        return None if channels is None else channels.bouquet_for

    def read(self):
        return read_service(self.session, self._bouquet_lookup())

    def refresh(self):
        payload = self.read()
        self._current = payload
        if payload is None:
            self.publish("service", EMPTY_SERVICE)
            return None
        self.publish("service", payload)
        self._check_zap(payload.get("sref"))
        return payload

    @property
    def sref(self):
        payload = self._current if self._current is not None else self.read()
        return None if payload is None else payload.get("sref")

    # ------------------------------------------------------- zap verification --

    def expect(self, sref):
        """Watch for a zap to land, and say so on `last_error` when it does not."""
        self._waiting_for = str(sref)
        self._verify.start(ZAP_VERIFY_MILLISECONDS, True)

    def _check_zap(self, sref):
        if self._waiting_for is None:
            return
        if same_service(self._waiting_for, sref):
            self._waiting_for = None
            self._verify.stop()

    def _zap_timed_out(self):
        wanted, self._waiting_for = self._waiting_for, None
        if wanted is None:
            return
        current = self.sref
        if same_service(wanted, current):
            return
        self.report(
            "zap",
            "the box did not tune to " + str(wanted) + " within "
            + str(ZAP_VERIFY_MILLISECONDS // 1000) + " s (it is on " + str(current) + ")",
        )

    def snapshot(self):
        # The shape is published even with nothing playing, so that a consumer
        # reading `service.sref` finds a null rather than a missing topic.
        payload = self.read()
        self._current = payload
        return {"service": payload if payload is not None else EMPTY_SERVICE}


class EpgPublisher(NavPublisher):
    """`epg` - the programme now and the one after it."""

    name = "epg"
    events = ("evUpdatedEventInfo", "evStart", "evEnd")

    def __init__(self, bridge=None):
        NavPublisher.__init__(self, bridge)
        self._boundary = Ticker(self.refresh, "programme boundary")

    def start(self):
        if not NavPublisher.start(self):
            return False
        self._arm(None)
        return True

    def stop(self):
        self._boundary.stop()
        NavPublisher.stop(self)

    def on_service_event(self, _event):
        self.refresh()

    def refresh(self):
        payload = read_epg(self.session)
        self.publish("epg", payload)
        self._arm(payload.get("now"))
        return payload

    def _arm(self, now):
        """Wake up when this programme ends - that is when the state changes.

        Without this the programme sensor of a box left on one channel shows the
        news until somebody zaps, which on a receiver nobody has touched since
        the morning is most of the day.
        """
        delay = EPG_MAX_SLEEP_MILLISECONDS
        if now:
            try:
                remaining = int(now.get("end", 0)) - int(time.time())
                # A second past the boundary, so the new programme is the one
                # enigma2 answers with.
                delay = max(EPG_MIN_SLEEP_MILLISECONDS, (remaining + 1) * 1000)
            except (TypeError, ValueError):
                delay = EPG_MAX_SLEEP_MILLISECONDS
        self._boundary.start(min(delay, EPG_MAX_SLEEP_MILLISECONDS), True)

    def snapshot(self):
        return {"epg": read_epg(self.session)}


class TunerPublisher(NavPublisher):
    """`tuner` - how well the signal is arriving."""

    name = "tuner"
    events = ("evTunedIn", "evTuneFailed", "evStart")

    def __init__(self, bridge=None):
        NavPublisher.__init__(self, bridge)
        self._ticker = Ticker(self.refresh, "tuner")

    def start(self):
        if frontend_constant("signalQuality") is None:
            return False
        if not NavPublisher.start(self):
            return False
        self._ticker.start(TUNER_POLL_MILLISECONDS)
        return True

    def stop(self):
        self._ticker.stop()
        NavPublisher.stop(self)

    def on_service_event(self, _event):
        self.refresh()

    def refresh(self):
        # Nothing tuned keeps the contract's shape, so that a consumer reading
        # `tuner.snr` finds a null rather than a missing key.
        payload = read_tuner(self.session) or EMPTY_TUNER
        self.publish("tuner", payload)
        return payload

    def snapshot(self):
        return {"tuner": read_tuner(self.session) or EMPTY_TUNER}
