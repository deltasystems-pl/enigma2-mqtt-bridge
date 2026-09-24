"""Recordings and the timers behind them.

Every recording on an Enigma2 box is a timer, including the one somebody started
by pressing the red button thirty seconds ago. So there is one source of truth
here - `session.nav.RecordTimer` - and two topics reading it for two different
questions: `recording` answers „is the box busy right now", which is what the
shutdown guards need, and `timers` answers „what is it going to do", which is
what a household planner needs.

The timer list has no change event. This build of enigma2 has no
`on_state_change` list to attach to, so what is wrapped instead is `saveTimer`,
which enigma2 calls after every change it makes to the list - adding, removing,
editing, a timer finishing. Wrapping it is how this plugin learns about a timer
somebody set from the remote control while sitting in front of the television.

The publish is coalesced by a quarter of a second, because a single edit calls
`saveTimer` more than once and a household does not need three identical
payloads on the broker to learn about one timer.
"""

import time

from .enigma2 import (
    Ticker,
    enigma_attribute,
    identity,
    missing,
    reference_string,
    service_reference,
)
from .log import get_logger
from .publisher import Publisher

LOG = get_logger("recording")

# Nothing may shut the box down with a recording this close. The contract says
# ten minutes and so does every guard that reads it.
GUARD_WINDOW_SECONDS = 600

# What an instant recording is worth if nobody stops it. The remote's record
# button offers the same two hours.
INSTANT_RECORD_SECONDS = 2 * 60 * 60

COALESCE_MILLISECONDS = 250

STATE_NAMES = (
    ("StateWaiting", "waiting"),
    ("StatePrepared", "prepared"),
    ("StateRunning", "running"),
    ("StateEnded", "ended"),
)


def record_timer(session):
    """`session.nav.RecordTimer`, or None."""
    nav = getattr(session, "nav", None) if session is not None else None
    if nav is None:
        return None
    timer = getattr(nav, "RecordTimer", None)
    if timer is None:
        missing("session.nav.RecordTimer")
    return timer


def _entry_class():
    try:
        from RecordTimer import RecordTimerEntry

        return RecordTimerEntry
    except Exception as error:
        missing("RecordTimer.RecordTimerEntry", error)
        return None


def _state_map():
    """enigma2's state numbers as the contract's words.

    Read off the class rather than assumed: the numbers are an implementation
    detail of a module that has been rewritten more than once, and the words are
    the interface.
    """
    entry = _entry_class()
    mapping = {}
    for index, (attribute, word) in enumerate(STATE_NAMES):
        value = getattr(entry, attribute, index) if entry is not None else index
        mapping[value] = word
    return mapping


def state_word(state):
    return _state_map().get(state, "waiting")


def _timer_sref(timer):
    reference = getattr(timer, "service_ref", None)
    return reference_string(reference)


def _timer_payload(timer):
    try:
        return {
            "name": str(getattr(timer, "name", "") or ""),
            "sref": _timer_sref(timer),
            "begin": int(getattr(timer, "begin", 0) or 0),
            "end": int(getattr(timer, "end", 0) or 0),
            "state": state_word(getattr(timer, "state", 0)),
            "repeated": int(getattr(timer, "repeated", 0) or 0),
        }
    except Exception:
        LOG.debug("a timer could not be read")
        return None


def timer_list(session):
    timer = record_timer(session)
    entries = getattr(timer, "timer_list", None) if timer is not None else None
    return list(entries or [])


def read_timers(session):
    """The `timers` payload - a list, one entry per timer."""
    payloads = []
    for timer in timer_list(session):
        payload = _timer_payload(timer)
        if payload is not None:
            payloads.append(payload)
    payloads.sort(key=lambda entry: entry["begin"])
    return payloads


def _is_running(timer):
    entry = _entry_class()
    running = getattr(entry, "StateRunning", 2) if entry is not None else 2
    return getattr(timer, "state", None) == running and not getattr(timer, "justplay", False)


def read_recording(session):
    """The `recording` payload - what is being written now, and what is next."""
    active = []
    upcoming = []
    now = int(time.time())
    for timer in timer_list(session):
        payload = _timer_payload(timer)
        if payload is None:
            continue
        if getattr(timer, "disabled", False):
            continue
        if _is_running(timer):
            active.append({key: payload[key] for key in ("name", "sref", "begin", "end")})
        elif payload["begin"] >= now and not getattr(timer, "justplay", False):
            upcoming.append({key: payload[key] for key in ("name", "sref", "begin", "end")})
    upcoming.sort(key=lambda entry: entry["begin"])
    return {"active": active, "next": upcoming[0] if upcoming else None}


def is_recording(session):
    timer = record_timer(session)
    checker = getattr(timer, "isRecording", None) if timer is not None else None
    if checker is None:
        # No answer is not „no": a box that will not say whether it is recording
        # is a box nothing here may shut down.
        return None
    try:
        return bool(checker())
    except Exception:
        LOG.exception("isRecording() raised")
        return None


def next_recording_time(session):
    timer = record_timer(session)
    getter = getattr(timer, "getNextRecordingTime", None) if timer is not None else None
    if getter is None:
        return None
    try:
        when = int(getter())
    except Exception:
        LOG.exception("getNextRecordingTime() raised")
        return None
    return when if when > 0 else None


def guard(session, window=GUARD_WINDOW_SECONDS):
    """Why the box must not be shut down right now, or None when it may be.

    Refusing when the answer cannot be read is the whole point: a shutdown that
    interrupts a recording cannot be undone, and „I could not tell" is not the
    same as „nothing is recording".
    """
    recording = is_recording(session)
    if recording is None:
        return "this image will not say whether it is recording; refusing to risk it"
    if recording:
        return "the receiver is recording"
    when = next_recording_time(session)
    if when is not None:
        remaining = when - int(time.time())
        if 0 <= remaining <= window:
            return "a recording starts in " + str(int(remaining)) + " s"
    return None


# ------------------------------------------------------------------- commands --


def _epg_cache():
    factory = enigma_attribute("eEPGCache")
    if factory is None:
        return None
    try:
        return factory.getInstance()
    except Exception:
        LOG.exception("eEPGCache.getInstance() raised")
        return None


def _service_wrapper(sref):
    try:
        from ServiceReference import ServiceReference
    except Exception as error:
        missing("ServiceReference", error)
        return None
    try:
        return ServiceReference(str(sref))
    except Exception:
        LOG.exception("could not wrap %s", sref)
        return None


def _record(session, entry):
    """Hand a built timer to enigma2. None on success, otherwise the refusal.

    `record()` answers with `None` when it took the timer and with a *list of
    conflicting timers* when it did not, which is a return value worth reading
    rather than a result worth ignoring.
    """
    timer = record_timer(session)
    recorder = getattr(timer, "record", None) if timer is not None else None
    if recorder is None:
        return "this image's RecordTimer has no record()"
    try:
        conflicts = recorder(entry)
    except Exception as error:
        LOG.exception("RecordTimer.record() raised")
        return type(error).__name__ + ": " + str(error)
    if conflicts:
        names = []
        for conflict in conflicts if isinstance(conflicts, (list, tuple)) else [conflicts]:
            names.append(str(getattr(conflict, "name", "") or "a timer"))
        return "the timer conflicts with " + ", ".join(names[:3])

    # 🔴 `record()` answers `None` for two different outcomes: the timer was
    # accepted, and the timer was silently dropped because its own duplicate
    # check recognised one like it. The return value cannot tell them apart, so
    # the list is the only honest answer - and „added" is exactly the kind of
    # claim that must be verified by effect rather than by a return code.
    if not any(listed is entry for listed in timer_list(session)):
        return "the receiver did not keep the timer; it already has one like it"
    return None


def add_event_timer(session, sref, event_id):
    """A timer for a programme enigma2 already knows about.

    This is the form to prefer: `parseEvent` gives the timer the programme's own
    name, description and the recording margins configured on the box, so the
    result is indistinguishable from one set with the remote control.
    """
    cache = _epg_cache()
    if cache is None:
        return "this image has no EPG cache"
    reference = service_reference(sref)
    if reference is None:
        return "'" + str(sref) + "' is not a service reference"
    try:
        event = cache.lookupEventId(reference, int(event_id))
    except Exception as error:
        LOG.exception("lookupEventId raised")
        return type(error).__name__ + ": " + str(error)
    if event is None:
        return "no event " + str(event_id) + " on " + str(sref)
    try:
        from RecordTimer import parseEvent
    except Exception as error:
        missing("RecordTimer.parseEvent", error)
        return "this image has no RecordTimer.parseEvent"
    entry_class = _entry_class()
    wrapper = _service_wrapper(sref)
    if entry_class is None or wrapper is None:
        return "this image cannot build a recording timer"
    try:
        entry = entry_class(wrapper, *parseEvent(event))
    except Exception as error:
        LOG.exception("could not build a timer from event %s", event_id)
        return type(error).__name__ + ": " + str(error)
    return _record(session, entry)


def add_manual_timer(session, sref, begin, end, name):
    """A timer for a window somebody chose themselves."""
    entry_class = _entry_class()
    wrapper = _service_wrapper(sref)
    if entry_class is None or wrapper is None:
        return "this image cannot build a recording timer"
    try:
        begin = int(begin)
        end = int(end)
    except (TypeError, ValueError):
        return "begin and end must be epoch seconds"
    if end <= begin:
        return "the timer ends before it begins"
    try:
        # The sixth argument is the EPG event id, and `0` is enigma2's own way
        # of saying „this timer is not attached to a programme". `None` is not:
        # the removal path compares event ids and would trip over it.
        entry = entry_class(wrapper, begin, end, str(name or ""), "", 0)
    except Exception as error:
        LOG.exception("could not build a manual timer")
        return type(error).__name__ + ": " + str(error)
    return _record(session, entry)


def find_timer(session, sref, begin, end):
    """The timer identified by service, start and end - enigma2's own identity."""
    try:
        begin = int(begin)
        end = int(end)
    except (TypeError, ValueError):
        return None
    wanted = identity(sref)
    for timer in timer_list(session):
        if identity(_timer_sref(timer)) != wanted:
            continue
        if int(getattr(timer, "begin", 0) or 0) != begin:
            continue
        if int(getattr(timer, "end", 0) or 0) != end:
            continue
        return timer
    return None


def delete_timer(session, sref, begin, end):
    found = find_timer(session, sref, begin, end)
    if found is None:
        return "no timer on " + str(sref) + " from " + str(begin) + " to " + str(end)
    timer = record_timer(session)
    remover = getattr(timer, "removeEntry", None) if timer is not None else None
    if remover is None:
        return "this image's RecordTimer has no removeEntry"
    try:
        remover(found)
    except Exception as error:
        LOG.exception("removeEntry raised")
        return type(error).__name__ + ": " + str(error)
    return None


def _current_reference(session):
    from .enigma2 import current_service_reference

    return current_service_reference(session)


def _current_title(session):
    from .service import read_epg

    now = read_epg(session).get("now") or {}
    return str(now.get("title") or "")


def start_instant_recording(session, duration=INSTANT_RECORD_SECONDS):
    """Record what is playing, now, for two hours unless stopped."""
    sref = _current_reference(session)
    if not sref:
        return "nothing is playing"
    begin = int(time.time())
    name = _current_title(session) or "instant record"
    return add_manual_timer(session, sref, begin, begin + int(duration), name)


def stop_instant_recording(session):
    """Stop the running recording of the service that is playing."""
    sref = identity(_current_reference(session))
    if not sref:
        return "nothing is playing"
    for timer in timer_list(session):
        if not _is_running(timer):
            continue
        if identity(_timer_sref(timer)) != sref:
            continue
        return delete_timer(
            session, _timer_sref(timer), getattr(timer, "begin", 0), getattr(timer, "end", 0)
        )
    return "nothing is being recorded on the service that is playing"


# ----------------------------------------------------------------- publishers --


class TimerWatcher(Publisher):
    """The half of a recording publisher that listens to the timer list."""

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._coalesce = Ticker(self.refresh, "timers")
        self._original_save = None
        self._wrapper = None
        self._timer = None
        self._nav = None

    MARKER = "_mqttbridge_listeners"
    ORIGINAL = "_mqttbridge_original"

    def start(self):
        timer = record_timer(self.session)
        if timer is None:
            return False
        self._timer = timer
        self._wrap_save()
        self._bind_record_event()
        return True

    def stop(self):
        self._coalesce.stop()
        self._unwrap_save()
        self._unbind_record_event()

    def soon(self):
        """Publish after the current burst of changes, not during it."""
        self._coalesce.start(COALESCE_MILLISECONDS, True)

    # `saveTimer` is enigma2's own „the list changed" - it writes timers.xml.
    #
    # Two publishers read that list, so the wrapper carries a list of listeners
    # rather than one callback. The alternative - whichever publisher started
    # first wrapping the method for itself - silently leaves the second one
    # listening to nothing, which is how `timers` would stop noticing a timer
    # set from the remote control while `recording` kept working.
    def _wrap_save(self):
        timer = self._timer
        original = getattr(timer, "saveTimer", None)
        if original is None:
            return
        listeners = getattr(original, self.MARKER, None)
        if listeners is not None:
            # Already wrapped, by our sibling. Join it.
            listeners.append(self.soon)
            self._wrapper = original
            return

        listeners = [self.soon]

        def wrapper(*args, **kwargs):
            result = original(*args, **kwargs)
            for listener in list(listeners):
                try:
                    listener()
                except Exception:
                    LOG.exception("publishing after a timer change raised")
            return result

        setattr(wrapper, self.MARKER, listeners)
        # On the wrapper rather than only on the publisher, so that whichever of
        # the two stops last can put the original back - which is not
        # necessarily the one that installed it.
        setattr(wrapper, self.ORIGINAL, original)
        try:
            timer.saveTimer = wrapper
        except Exception:
            LOG.exception("could not wrap RecordTimer.saveTimer")
            return
        self._original_save = original
        self._wrapper = wrapper

    def _unwrap_save(self):
        self._original_save = None
        wrapper, self._wrapper = self._wrapper, None
        timer, self._timer = self._timer, None
        listeners = getattr(wrapper, self.MARKER, None)
        if listeners is not None and self.soon in listeners:
            listeners.remove(self.soon)
        if timer is None or listeners is None or listeners:
            # Nothing to restore, or our sibling is still listening.
            return
        original = getattr(wrapper, self.ORIGINAL, None)
        if original is None:
            return
        try:
            if getattr(getattr(timer, "saveTimer", None), self.MARKER, None) is not None:
                timer.saveTimer = original
        except Exception:
            LOG.exception("could not restore RecordTimer.saveTimer")

    # `record_event` is the recording itself starting, ending, or failing to
    # write - which `saveTimer` does not always cover.
    def _bind_record_event(self):
        nav = getattr(self.session, "nav", None) if self.session is not None else None
        hook = getattr(nav, "record_event", None) if nav is not None else None
        if hook is None:
            missing("session.nav.record_event")
            return
        try:
            hook.append(self._on_record_event)
            self._nav = nav
        except Exception as error:
            missing("session.nav.record_event", error)

    def _unbind_record_event(self):
        nav, self._nav = self._nav, None
        if nav is None:
            return
        try:
            hook = getattr(nav, "record_event", None)
            if hook is not None and self._on_record_event in hook:
                hook.remove(self._on_record_event)
        except Exception:
            LOG.debug("could not detach from session.nav.record_event")

    def _on_record_event(self, _service=None, _event=None):
        try:
            self.soon()
        except Exception:
            LOG.exception("a record event raised")

    def refresh(self):
        raise NotImplementedError


class RecordingPublisher(TimerWatcher):
    """`recording` - what is being written to the disk, and what is next."""

    name = "recording"

    def refresh(self):
        payload = read_recording(self.session)
        self.publish("recording", payload)
        return payload

    def snapshot(self):
        return {"recording": read_recording(self.session)}


class TimersPublisher(TimerWatcher):
    """`timers` - the whole list, as a list."""

    name = "timers"

    def refresh(self):
        payload = read_timers(self.session)
        self.publish("timers", payload)
        return payload

    def snapshot(self):
        return {"timers": read_timers(self.session)}
