"""The doorway to enigma2's own API - the only place in this package that guesses.

Nothing here assumes an image provides anything. Every name is imported inside a
function, every call is wrapped, and a capability that cannot be bound says so
**once** and then stays quiet: a receiver that logs the same missing attribute
every sixty seconds fills its flash with one sentence.

Two things live here rather than in the publishers that use them.

`Ticker` is an `eTimer` that cannot raise into the main loop. enigma2's timer
calls straight back into Python on the thread that draws the television, so a
callback that raises there is a traceback in the middle of the user interface.

The service-reference helpers exist because a service reference is spelled
differently depending on who hands it to you. The same channel is
`1:0:19:283D:3FB:1:C00000:0:0:0:` from `getCurrentlyPlayingServiceReference()`
and `1:0:19:283D:3FB:1:C00000:0:0:0::Channel Name` out of a bouquet, and a naive
string comparison of the two says they are different services.
"""

from .log import get_logger

LOG = get_logger("enigma2")

# Eleven fields, which is ten numbers and the path. The first ten are the
# reference proper - type, flags, service type, sid, tsid, onid, namespace and
# the three parent fields - and everything past the eleventh is the name the
# bouquet gave it, which is decoration.
#
# The eleventh field is included because dropping it would make every stream on
# a box identical to every other: an IPTV service is
# `4097:0:1:0:0:0:0:0:0:0:http%3a//...`, where the ten numbers are the same for
# all of them and the URL in field eleven is the only thing that differs. It is
# also the number enigma2's own timer removal normalises to.
SERVICE_FIELDS = 11

_missing = set()


def missing(what, error=None):
    """Note a name this image does not provide. True the first time only."""
    if what in _missing:
        return False
    _missing.add(what)
    if error is None:
        LOG.warning("this image does not provide %s", what)
    else:
        LOG.warning("this image does not provide %s (%s: %s)", what, type(error).__name__, error)
    return True


def forget_missing():
    """Test seam: the set above is module state and outlives a test."""
    _missing.clear()


# ------------------------------------------------------------------- imports --


def enigma():
    """The `enigma` module, or None off a receiver."""
    try:
        import enigma

        return enigma
    except Exception as error:  # pragma: no cover - only on a broken image
        missing("the enigma module", error)
        return None


def enigma_attribute(name):
    """One name out of `enigma`, or None."""
    module = enigma()
    if module is None:
        return None
    found = getattr(module, name, None)
    if found is None:
        missing("enigma." + name)
    return found


def constant(class_name, attribute, default=None):
    """`enigma.<class_name>.<attribute>` - an enum value, or `default`.

    The interface classes (`iServiceInformation`, `iPlayableService`, ...) are
    where enigma2 keeps its enumerations. Their *values* differ between images
    and must never be hard-coded; their *names* have been stable for years.
    """
    owner = enigma_attribute(class_name)
    if owner is None:
        return default
    value = getattr(owner, attribute, None)
    if value is None:
        missing(class_name + "." + attribute)
        return default
    return value


# -------------------------------------------------------------------- timers --


class Ticker:
    """An `eTimer` whose callback cannot raise into enigma2's main loop.

    The timer object is created on the first `start`, so a `Ticker` can be built
    on a machine with no `enigma` module at all - which is every machine the
    tests run on, and every machine a reviewer reads this on.
    """

    def __init__(self, callback, name="timer"):
        self._callback = callback
        self._name = name
        self._timer = None
        self._firing = False
        self._detach_wanted = False

    @property
    def timer(self):
        return self._timer

    def _fire(self):
        self._firing = True
        try:
            self._callback()
        except Exception:
            LOG.exception("the %s timer raised", self._name)
        finally:
            self._firing = False
            if self._detach_wanted:
                self._detach()

    def _build(self):
        factory = enigma_attribute("eTimer")
        if factory is None:
            return None
        try:
            timer = factory()
        except Exception:
            LOG.exception("could not create the %s timer", self._name)
            return None
        # Newer images expose `callback` as a plain list; older ones only have
        # the signal object, whose listener list is behind `get()`.
        handle = getattr(timer, "callback", None)
        try:
            if handle is not None:
                handle.append(self._fire)
            else:
                timer.timeout.get().append(self._fire)
        except Exception:
            LOG.exception("could not attach the %s timer", self._name)
            return None
        return timer

    def start(self, milliseconds, single=False):
        # A callback that stops this ticker and starts it again in the same
        # turn has already asked for a deferred detach. It just changed its
        # mind, and detaching after `_fire` returns would undo the restart.
        self._detach_wanted = False
        if self._timer is None:
            self._timer = self._build()
        if self._timer is None:
            return False
        try:
            self._timer.start(int(milliseconds), bool(single))
        except Exception:
            LOG.exception("could not start the %s timer", self._name)
            return False
        return True

    def stop(self):
        if self._timer is None:
            return False
        try:
            self._timer.stop()
        except Exception:
            LOG.exception("could not stop the %s timer", self._name)
            return False
        if self._firing:
            # Called from inside our own callback - which is what a publisher
            # that gives up on a poll does. The timer is walking its callback
            # list right now, so the detach waits for `_fire` to return.
            self._detach_wanted = True
            return True
        self._detach()
        return True

    def _detach(self):
        """Give the timer back: our callback off its list, our reference gone.

        A stopped timer that still holds a bound method of the object that
        owns it keeps that object reachable from enigma2's side. `start` builds
        a fresh timer, so nothing needs this one afterwards.
        """
        timer, self._timer = self._timer, None
        self._detach_wanted = False
        if timer is None:
            return
        try:
            handle = getattr(timer, "callback", None)
            if handle is not None:
                handle.remove(self._fire)
            else:
                timer.timeout.get().remove(self._fire)
        except Exception:
            LOG.debug("could not detach the %s timer's callback", self._name)


# --------------------------------------------------------- service references --


def service_reference(sref):
    """An `eServiceReference` from its string form, or None."""
    factory = enigma_attribute("eServiceReference")
    if factory is None:
        return None
    try:
        return factory(str(sref))
    except Exception:
        LOG.exception("could not build a service reference from %r", sref)
        return None


def reference_string(reference):
    """The string form of whatever enigma2 handed us - reference, wrapper or text."""
    if reference is None:
        return ""
    to_string = getattr(reference, "toString", None)
    if to_string is not None:
        try:
            return str(to_string())
        except Exception:
            return ""
    inner = getattr(reference, "ref", None)
    if inner is not None and inner is not reference:
        return reference_string(inner)
    return str(reference)


def identity(sref):
    """The fields that identify a service, upper-cased and padded for comparison.

    Padded because the two spellings of the same channel differ in how many
    fields they have: one ends at the tenth colon and the other carries a name
    after it. Comparing the padded forms makes those equal without making two
    different streams equal.
    """
    parts = [part.strip().upper() for part in str(sref or "").strip().split(":")]
    if not any(parts):
        return ""
    parts = parts[:SERVICE_FIELDS]
    parts += [""] * (SERVICE_FIELDS - len(parts))
    return ":".join(parts)


def same_service(one, other):
    left = identity(one)
    return bool(left) and left == identity(other)


def service_name(sref):
    """The channel name for a reference, through enigma2's own resolver."""
    try:
        from ServiceReference import ServiceReference
    except Exception as error:
        missing("ServiceReference", error)
        return ""
    try:
        return str(ServiceReference(str(sref)).getServiceName() or "")
    except Exception:
        LOG.debug("could not resolve a name for %s", sref)
        return ""


# ------------------------------------------------------------------- session --


def navigation(session):
    """`session.nav`, or None when there is no session (tests, early start-up)."""
    if session is None:
        return None
    return getattr(session, "nav", None)


def current_service(session):
    """The service being played, or None."""
    nav = navigation(session)
    if nav is None:
        return None
    getter = getattr(nav, "getCurrentService", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        LOG.exception("could not read the current service")
        return None


def current_service_reference(session):
    """The string form of what is playing, or an empty string."""
    nav = navigation(session)
    if nav is None:
        return ""
    getter = getattr(nav, "getCurrentlyPlayingServiceReference", None)
    if getter is None:
        return ""
    try:
        return reference_string(getter())
    except Exception:
        LOG.exception("could not read the playing service reference")
        return ""


def append_listener(owner, attribute, listener):
    """Append to one of enigma2's plain-list event hooks. True when it took."""
    if owner is None:
        return False
    hook = getattr(owner, attribute, None)
    if hook is None:
        missing(attribute)
        return False
    try:
        hook.append(listener)
    except Exception as error:
        missing(attribute, error)
        return False
    return True


def remove_listener(owner, attribute, listener):
    if owner is None:
        return False
    hook = getattr(owner, attribute, None)
    if hook is None:
        return False
    try:
        if listener in hook:
            hook.remove(listener)
            return True
    except Exception:
        LOG.debug("could not detach from %s", attribute)
    return False
