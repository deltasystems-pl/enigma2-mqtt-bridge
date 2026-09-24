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


def _zap_through_channel_list(reference):
    """Zap the way the remote control does, when the service is in view.

    Worth the trouble because `playService` alone leaves the receiver's own
    channel list where it was: tune to BBC One from Home Assistant, press
    channel-up on the remote, and the box goes to the neighbour of whatever was
    selected before - not the neighbour of what is on screen.

    🔴 `zap()` tunes to whatever the list has *selected*, so the selection is
    read back and compared before it is called. A `setCurrentSelection` for a
    service that is not in the list the user is browsing does nothing at all,
    and calling `zap()` after one would tune the television to the wrong
    channel. That is why this returns False rather than trying harder: the
    caller's `playService` is always correct, and this is only ever an
    improvement on it.
    """
    try:
        from Screens.InfoBar import InfoBar
    except Exception:
        return False
    infobar = getattr(InfoBar, "instance", None)
    servicelist = getattr(infobar, "servicelist", None) if infobar is not None else None
    if servicelist is None:
        return False
    select = getattr(servicelist, "setCurrentSelection", None)
    current = getattr(servicelist, "getCurrentSelection", None)
    zapper = getattr(servicelist, "zap", None)
    if select is None or current is None or zapper is None:
        return False
    try:
        select(reference)
        if not same_service(reference_string(current()), reference_string(reference)):
            return False
        zapper()
    except Exception:
        LOG.debug("the channel list would not take the zap; using playService")
        return False
    return True


def zap(session, sref):
    """Tune to a service. None on success, otherwise the refusal.

    Standby first: the channel list is not usable in standby, and a box woken
    afterwards restores the service it was on - so a zap sent to a sleeping
    receiver would silently undo itself.
    """
    nav = navigation(session)
    if nav is None:
        return "there is no session to zap with"
    player = getattr(nav, "playService", None)
    if player is None:
        return "this image's navigation has no playService"
    reference = service_reference(sref)
    if reference is None:
        return "'" + str(sref) + "' is not a service reference"

    from .power import in_standby, wake

    was_asleep = bool(in_standby())
    error = wake()
    if error:
        LOG.info("could not leave standby before zapping: %s", error)

    if not was_asleep and _zap_through_channel_list(reference):
        return None

    try:
        player(reference)
    except Exception as error:
        LOG.exception("playService raised")
        return type(error).__name__ + ": " + str(error)
    return None


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
