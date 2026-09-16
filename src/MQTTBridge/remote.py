"""The remote control: watching it, and pretending to be it.

🔴 **The handler returns 0, always, on every path.** enigma2 asks every bound
action handler whether it consumed the key, and anything other than `0` means
„yes, I dealt with it" — the key never reaches the user interface and the person
holding the remote finds that their television has stopped responding. This
plugin observes; it never consumes, and it never *acts* on a key either.
`KEY_POWER` is published like any other and interpreted by nobody here.

The press model is deliberately not the flag model. Physical OpenViX input reports
a make, repeats while the button is held, then a break; synthetic input can also
report a long marker. A household automation wants one event per press with „was
it held?" attached, so the press is published once at the terminal marker or
break. Repeated input held for at least a second is the conservative fallback.

The publish rate is capped. A remote whose button is stuck, or a child leaning on
the handset, would otherwise turn into a hundred retained-free publishes a second
for as long as it lasts.
"""

import sys
import time

from .enigma2 import enigma_attribute, missing
from .keys import PRESS_LONG, PRESS_SHORT, code_for, name_for
from .log import get_logger
from .publisher import Publisher

LOG = get_logger("remote")

# enigma2's own flag numbering, from the driver up.
FLAG_MAKE = 0
FLAG_BREAK = 1
FLAG_REPEAT = 2
FLAG_LONG = 3

# The contract's cap, counted over a sliding second.
MAX_PUBLISHES_PER_SECOND = 20
RATE_WINDOW_SECONDS = 1.0

# OpenViX starts repeats after 500 ms by default. One repeat can therefore still
# be an ordinary tap; require twice that interval before using repeats as the
# fallback for a missing long marker.
LONG_PRESS_FALLBACK_SECONDS = 1.0

# A priority low enough that every other handler in the receiver is offered the
# key first. `-maxsize - 1` is the most negative integer this Python has, which
# is how a plugin says „last, and never in anybody's way".
BIND_PRIORITY = -sys.maxsize - 1

# The device a synthesised key press claims to come from. It is not decoration:
# enigma2 dispatches the press as if that input device had produced it, and a
# name no driver ever registered is a press that arrives nowhere. This is the
# name OpenWebif's own remote control uses for every box by default, which makes
# it the most-exercised string in the whole of Enigma2.
DEVICE = "dreambox remote control (native)"


class RateLimiter:
    """At most `limit` things a second, and one log line about the rest.

    Used twice, for the two directions: presses observed on the remote and
    presses injected into the receiver. The second is not a nicety — an
    automation stuck in a loop would otherwise hand enigma2 a key press every
    few milliseconds for as long as it ran.
    """

    def __init__(self, limit, what):
        self.limit = limit
        self.what = what
        self._recent = []
        self._dropped = 0

    def forget(self):
        self._recent = []
        self._dropped = 0

    def allowed(self):
        now = time.time()
        cutoff = now - RATE_WINDOW_SECONDS
        self._recent = [stamp for stamp in self._recent if stamp > cutoff]
        if len(self._recent) >= self.limit:
            self._dropped += 1
            if self._dropped == 1:
                LOG.warning(
                    "more than %d %s a second; dropping the rest", self.limit, self.what
                )
            return False
        if self._dropped:
            LOG.info("dropped %d %s over the rate limit", self._dropped, self.what)
            self._dropped = 0
        self._recent.append(now)
        return True


_injected = RateLimiter(MAX_PUBLISHES_PER_SECOND, "injected key presses")


def forget_rate_limit():
    """Test seam: the injection limiter is module state and outlives a test."""
    _injected.forget()


def action_map():
    factory = enigma_attribute("eActionMap")
    if factory is None:
        return None
    try:
        return factory.getInstance()
    except Exception:
        LOG.exception("eActionMap.getInstance() raised")
        return None


def press(name, long=False):
    """Inject a key press. None on success, otherwise the refusal.

    A long press is a make, the long marker, and then a break — the same three
    events the driver produces for a held button, which is what makes a plugin
    that only listens to the break see it as a long press.

    A press dropped by the rate limit is **not** a refusal: it is logged and the
    command counts as done, because a flood would otherwise fill `last_error`
    twenty times a second with the news that it is a flood.
    """
    code = code_for(name)
    if code is None:
        return "unknown key '" + str(name) + "'"
    actions = action_map()
    if actions is None:
        return "this image has no eActionMap"
    if not _injected.allowed():
        return None
    sequence = [FLAG_MAKE, FLAG_LONG, FLAG_BREAK] if long else [FLAG_MAKE, FLAG_BREAK]
    for flag in sequence:
        error = _key_pressed(actions, code, flag)
        if error is not None:
            return error
    return None


def _key_pressed(actions, code, flag):
    """`keyPressed`, on both of the signatures enigma2 has shipped."""
    try:
        actions.keyPressed(DEVICE, int(code), int(flag))
        return None
    except TypeError:
        pass
    except Exception as error:
        LOG.exception("keyPressed(%s, %s) raised", code, flag)
        return type(error).__name__ + ": " + str(error)
    try:
        # The older two-argument form, before the device name was added.
        actions.keyPressed(int(code), int(flag))
        return None
    except Exception as error:
        LOG.exception("keyPressed(%s, %s) raised", code, flag)
        return type(error).__name__ + ": " + str(error)


class KeyPublisher(Publisher):
    """`key` — one payload per press, and never retained.

    Not retained on purpose: a retained key press is delivered again the moment
    anything subscribes, so every automation bound to the blue button would fire
    on every reconnect, forever.
    """

    name = "keys"

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._held = {}
        self._finished = set()
        self._limiter = RateLimiter(MAX_PUBLISHES_PER_SECOND, "key presses")
        # 🔴 One object, kept for the life of the publisher. `self._on_key` is a
        # *new* bound method every time it is read, and the action map holds the
        # object it was given and compares it by identity when unbinding — so
        # binding one and unbinding another leaves the handler attached for the
        # rest of the receiver's uptime.
        self._handler = self._on_key
        self._bound = False

    def start(self):
        if not self.value("publish_keys"):
            LOG.info("publish_keys is off; the remote is not watched")
            return False
        actions = action_map()
        if actions is None:
            return False
        binder = getattr(actions, "bindAction", None)
        if binder is None:
            missing("eActionMap.bindAction")
            return False
        try:
            binder("", BIND_PRIORITY, self._handler)
        except Exception as error:
            missing("eActionMap.bindAction", error)
            return False
        self._bound = True
        return True

    def stop(self):
        self._held.clear()
        self._finished.clear()
        if not self._bound:
            return
        self._bound = False
        actions = action_map()
        unbinder = getattr(actions, "unbindAction", None) if actions is not None else None
        if unbinder is None:
            LOG.debug("this image has no eActionMap.unbindAction")
            return
        try:
            unbinder("", self._handler)
        except Exception:
            LOG.exception("could not unbind from the action map")

    # --------------------------------------------------------------- the hook --

    def _on_key(self, key, flag):
        """enigma2's handler contract: the return value decides who gets the key.

        Every path here returns 0, including the one that catches an exception,
        because the alternative is a receiver whose remote does nothing.
        """
        try:
            self._handle(key, flag)
        except Exception:
            LOG.exception("the key handler raised")
        return 0

    def _handle(self, key, flag):
        code = int(key)
        flag = int(flag)
        if flag == FLAG_MAKE:
            self._finished.discard(code)
            self._held[code] = {
                "started": time.monotonic(),
                "repeated": False,
            }
            return
        if flag == FLAG_LONG:
            if self._held.pop(code, None) is not None:
                # OpenViX's action map does not offer BREAK to this binding
                # after it has offered LONG, so LONG is itself terminal.
                self._finished.add(code)
                self._emit(code, PRESS_LONG)
            return
        if flag == FLAG_REPEAT:
            # A held button, reported many times a second. The press is
            # published once, at the break.
            if code in self._held:
                self._held[code]["repeated"] = True
            return
        if flag != FLAG_BREAK:
            return
        if code in self._finished:
            # Some forks and the test action map do still deliver the break.
            self._finished.remove(code)
            return
        held = self._held.pop(code, None)
        was_long = False
        if held is not None:
            elapsed = time.monotonic() - held["started"]
            was_long = held["repeated"] and elapsed >= LONG_PRESS_FALLBACK_SECONDS
        self._emit(code, PRESS_LONG if was_long else PRESS_SHORT)

    # --------------------------------------------------------------- publishing --

    def _emit(self, code, press_kind):
        if not self._limiter.allowed():
            return
        payload = {"key": name_for(code) or ("KEY_" + str(code)), "press": press_kind}
        if self.bridge is not None:
            self.bridge.publish_state("key", payload, retain=False)

    def snapshot(self):
        # There is no state to snapshot: a key press is an event, and the last
        # one is not what the remote is doing now.
        return {}
