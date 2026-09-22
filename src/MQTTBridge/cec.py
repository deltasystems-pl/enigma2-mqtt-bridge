"""An opt-in workaround for a standby the television asks for and the receiver sits on.

**The defect is upstream enigma2's, not this plugin's**, and it is two defects
that happen to meet.

The first is **the wait**. When the television switches itself off it tells the
receiver over HDMI-CEC, and the image's `HdmiCec` answers by *queueing* a standby:
`Notifications.AddNotification(Screens.Standby.Standby)`. That queue is drained by
the info bar, and only while the info bar is the screen executing — its
`notificationAdded` callback returns at once otherwise. With the channel list
open, the standby therefore waits, and fires whenever somebody next closes the
list: a minute later, or an hour.

The second is **the echo**. `HdmiCec` marks the standby as the television's own
with `handlingStandbyFromTV`, and that flag is what stops the receiver sending
`<Standby>` back to the television when it enters standby. But the flag is set
and cleared around the *queueing* call rather than around the standby itself:
`True`, queue it, `False`. When the queue is drained at once — the ordinary case —
the whole chain runs inside that bracket and the flag holds. When the standby
waits, the chain runs long after the bracket closed, the receiver takes it for a
standby of its own, and tells the television to switch off.

**What this module does, in two parts.**

1. When the television's standby is queued behind the **channel list**, it
   closes the list through the list's own exit, so the queued standby proceeds
   now instead of whenever somebody next presses EXIT.
2. When that standby is still queued thirty seconds later — because something
   other than the channel list was open, and this module left it alone — it
   drops the entry from the queue, so it cannot fire hours later when somebody
   closes the menu they were in and take the television with it.

**Why the standby is identified at the moment it is queued.** A standby from the
plugin's own `cmd/power standby`, and one from the remote's power button, queue a
notification identical to the television's, byte for byte. Anything that looked
at the queue later for „a standby" could cancel one the household had just asked
for. The one moment the television's standby can be told apart is while it is
being queued: the image calls every `notificationAdded` callback from inside
`__AddNotification`, which runs inside `HdmiCec.standby()`, which runs inside the
`handlingStandbyFromTV` bracket. So the flag reads `True` there **if and only if**
the television queued the entry. The entry is then kept **by identity** — the
tuple itself, not its `id()` — and no other entry is ever touched.

**Why the hook is the image's own list.** `notificationAdded` is the extension
point the info bar itself registers on, and appending a callable to it is the
smallest possible footprint. Wrapping `AddNotification` would work as well and is
not done: replacing a function the whole interface calls is a larger blast
radius than joining a list it already iterates.

**Why the flag is held rather than the close made fast.** Closing a screen in
enigma2 does not pop it. `Session.close()` starts a zero-millisecond timer and
returns, and the pop — and with it the info bar resuming and draining the queue —
happens on a later turn of the main loop, by which time `messageReceived` has long
since put the flag back to `False`. However quickly the list is closed, the
standby it releases runs outside the bracket. So immediately before closing, the
flag is set `True` and its previous value remembered, and it is put back once the
standby has happened — or after five seconds, whichever comes first. The deadline
is there because a hold that leaked would silently stop „switch the receiver to
standby and the television goes off too" for the rest of the session, which a
household would notice and nobody would connect to this feature.

🔴 **Put back after the image has read it, never inside the notification.** The
image reads the flag from its own notifier on `config.misc.standbyCounter`, the
same list this module watches. Putting the value back from inside that
notification would make the result depend on which of the two notifiers happens to
be first in the list, and in the wrong order would produce exactly the echo this
module exists to stop. The restore therefore waits `SETTLE_MILLISECONDS` after the
counter moves — longer than the one second the image itself waits before sending
when it is configured to look for other receivers on the bus, so that deferred
read is covered too.

🔴 **Only the channel list, and only when it is the screen in front.** The
allowlist is three class names matched against the dialog's class and its bases,
and nothing else: closing a screen somebody is typing in, or a picker another
dialog is waiting on, would be a worse bug than the one being fixed. Only
`session.current_dialog` is ever looked at — `Session.close()` asserts that the
screen being closed is that one — and the stack underneath it never is.

**The power-on trigger is deferred.** The obvious rule for part 2 — drop the
stale standby when the television reports it is on again — has no event to hang
on: the image discards every wake-up message while the receiver is awake, which
is the whole premise of a stale standby. Reaching it would need the raw CEC
signal and a guess at which message this particular television sends, which the
image itself declines to guess (it offers a nine-way setting for it). A deadline
guesses nothing.

With `cec_standby_workaround` off, which is the default, none of this runs: no
callback is registered, nothing of the image's is read or written, and there is no
capability and no topic.
"""

import time

from .enigma2 import Ticker, missing
from .log import get_logger
from .publisher import Publisher

LOG = get_logger("cec")

TOPIC = "cec"

# 🔴 Exactly these three, matched against the dialog's class name and the names
# in its MRO. `ChannelSelectionBase` is deliberately absent although both real
# channel lists inherit it: `SimpleChannelSelection` inherits it too, and that is
# the service *picker* embedded in the timer editor, stream-relay setup and
# several plugins — closing it answers somebody's half-finished question with
# „nothing". Nothing else from the channel list's module is here either: the
# context menu, the bouquet selectors and the zap history are all separate
# screens stacked on top of the list, and are left alone.
ALLOWLIST = ("ChannelSelection", "ChannelSelectionRadio", "PiPZapSelection")

# How long an identified standby may wait in the queue before it is dropped.
STALE_SECONDS = 30

# The longest the echo is held suppressed when the standby never happens.
HOLD_SECONDS = 5

# How long after the standby counter moves the flag is put back. The image reads
# it either synchronously in its own notifier on that counter or, when it is set
# to look for other receivers on the bus, from a one-second timer it starts
# there. Waiting past both means the order of the notifiers cannot matter.
SETTLE_MILLISECONDS = 1500

KIND_CLOSED = "closed_channel_list"
KIND_DROPPED = "dropped_stale_standby"


def allowlisted(dialog):
    """Whether `dialog` is one of the channel lists, by its class or a base's name."""
    if dialog is None:
        return False
    try:
        names = [klass.__name__ for klass in type(dialog).__mro__]
    except Exception:
        return False
    return any(name in ALLOWLIST for name in names)


def _dialog_name(dialog):
    try:
        return type(dialog).__name__
    except Exception:
        return "?"


class _Queued:
    """One standby the television queued, held by the queue entry itself."""

    def __init__(self, entry, deadline):
        self.entry = entry
        self.queued_at = time.time()
        # One-shot: whatever happens, this standby causes at most one close.
        self.acted = False
        self.deadline = deadline


class CecPublisher(Publisher):
    """`cec` — what the standby workaround did, and whether a standby is waiting."""

    name = "cec_workaround"

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._notifications = None
        self._standby_screen = None
        self._cec_class = None
        self._counter = None
        self._bound = False

        self._queued = []
        self._hold = None

        self._last_intervention = None
        self._kind = None
        self._count = 0
        self._callback_failed = False

        self._act = Ticker(self._act_now, "cec standby")
        self._settle = Ticker(self._after_standby, "cec settle")
        self._hold_deadline = Ticker(self._release_hold, "cec hold")

    # ---------------------------------------------------------------- lifecycle --

    def start(self):
        # 🔴 The setting is read before anything of the image's is imported or
        # touched: switched off means switched off, not „bound and idle".
        if not self.value("cec_standby_workaround"):
            self.switched_off = True
            LOG.info("cec_standby_workaround is off; the CEC standby workaround is not bound")
            return False
        if not self._resolve():
            return False
        try:
            self._notifications.notificationAdded.append(self._notification_added)
        except Exception as error:
            missing("Tools.Notifications.notificationAdded", error)
            return False
        self._bound = True
        self._counter = self._bind_counter()
        LOG.info("the CEC standby workaround is bound")
        return True

    def stop(self):
        """Let go of everything, and give the flag back now if it is being held."""
        if self._bound:
            self._bound = False
            try:
                hooks = self._notifications.notificationAdded
                if self._notification_added in hooks:
                    hooks.remove(self._notification_added)
            except Exception:
                LOG.debug("could not detach from the notification queue")
        counter, self._counter = self._counter, None
        if counter is not None:
            try:
                remover = getattr(counter, "removeNotifier", None)
                if remover is not None:
                    remover(self._counter_moved)
                elif self._counter_moved in getattr(counter, "notifiers", []):
                    counter.notifiers.remove(self._counter_moved)
            except Exception:
                LOG.debug("could not detach from the standby counter")
        self._act.stop()
        self._settle.stop()
        self._hold_deadline.stop()
        for queued in self._queued:
            queued.deadline.stop()
        if self._queued:
            LOG.info("stopping with %d television standby(s) still queued; leaving them to the "
                     "image", len(self._queued))
        self._queued = []
        self._release_hold()

    def _resolve(self):
        """The three things this needs from the image. All of them, or no capability."""
        try:
            import Tools.Notifications as notifications
        except Exception as error:
            missing("Tools.Notifications", error)
            return False
        if not isinstance(getattr(notifications, "notificationAdded", None), list):
            missing("Tools.Notifications.notificationAdded")
            return False
        if not isinstance(getattr(notifications, "notifications", None), list):
            missing("Tools.Notifications.notifications")
            return False
        try:
            import Screens.Standby as standby
        except Exception as error:
            missing("Screens.Standby", error)
            return False
        screen = getattr(standby, "Standby", None)
        if screen is None:
            missing("Screens.Standby.Standby")
            return False
        try:
            from Components.HdmiCec import HdmiCec
        except Exception as error:
            missing("Components.HdmiCec", error)
            return False
        if getattr(HdmiCec, "instance", None) is None:
            # An image without HDMI-CEC, or one with it switched off so the
            # singleton was never built. A hook that can never fire is not a
            # capability.
            LOG.info("HDMI-CEC is not running on this receiver; the standby workaround has "
                     "nothing to work around")
            self.switched_off = True
            return False
        self._notifications = notifications
        self._standby_screen = screen
        self._cec_class = HdmiCec
        return True

    def _bind_counter(self):
        """The standby counter, which says when the standby actually happened.

        Without it the workaround still works; the hold is released by its
        deadline instead and a drained standby is noticed at its own deadline.
        """
        try:
            from Components.config import config

            counter = config.misc.standbyCounter
            notifier = counter.addNotifier
        except Exception as error:
            missing("config.misc.standbyCounter.addNotifier", error)
            return None
        try:
            # `initial_call=False`: this is an event, and the counter's current
            # value is not one.
            notifier(self._counter_moved, initial_call=False)
        except TypeError:
            try:
                notifier(self._counter_moved)
            except Exception as error:
                missing("config.misc.standbyCounter.addNotifier", error)
                return None
        except Exception as error:
            missing("config.misc.standbyCounter.addNotifier", error)
            return None
        return counter

    # -------------------------------------------------------------- identifying --

    def _notification_added(self):
        """Called by the image for every notification it queues — every popup too.

        So it has to be cheap and it has to be quiet: a screen other than the
        standby screen returns after one comparison, and nothing in here may
        raise into `__AddNotification`, which is iterating its callbacks.
        """
        try:
            queue = self._notifications.notifications
            if not queue:
                # Another callback — the info bar's — already drained it.
                return
            entry = queue[-1]
            if entry[1] is not self._standby_screen:
                return
            instance = self._cec_class.instance
            if instance is None or getattr(instance, "handlingStandbyFromTV", False) is not True:
                # The household's own standby — the remote, `cmd/power`. Never ours.
                return
            if any(queued.entry is entry for queued in self._queued):
                return
            deadline = Ticker(lambda: self._stale(entry), "cec stale standby")
            self._queued.append(_Queued(entry, deadline))
            deadline.start(STALE_SECONDS * 1000, True)
            # Acted on at the next turn of the main loop rather than here, inside
            # the television's own message handler: when the info bar is the
            # screen in front it drains the entry in this same turn, and by then
            # there is nothing to do and nothing worth saying.
            self._act.start(0, True)
            LOG.debug("a standby from the television was queued")
        except Exception:
            if not self._callback_failed:
                self._callback_failed = True
                LOG.exception("the CEC standby hook raised; the notification is unaffected")
            else:
                LOG.debug("the CEC standby hook raised again")

    # -------------------------------------------------------------------- acting --

    def _still_queued(self, entry):
        try:
            return any(item is entry for item in self._notifications.notifications)
        except Exception:
            return False

    def _act_now(self):
        """Part 1: close the channel list, if it is the screen holding the standby up."""
        waiting = [queued for queued in self._queued if not queued.acted]
        if not waiting:
            return
        drained = [queued for queued in waiting if not self._still_queued(queued.entry)]
        for queued in drained:
            # The info bar took it in the same turn: the ordinary, working case.
            self._forget(queued)
        waiting = [queued for queued in waiting if queued not in drained]
        if not waiting:
            self._publish()
            return
        for queued in waiting:
            queued.acted = True
        self._publish()
        self._close_the_channel_list()

    def _close_the_channel_list(self):
        session = self.session
        try:
            if session is None or not getattr(session, "in_exec", False):
                LOG.debug("no dialog is executing; leaving the queued standby to its deadline")
                return
            # 🔴 Read now, immediately before acting, and never anything below it.
            dialog = session.current_dialog
        except Exception:
            LOG.exception("could not read the current dialog; leaving it alone")
            return
        if not allowlisted(dialog):
            LOG.debug("a standby from the television is waiting behind %s, which is not the "
                      "channel list; leaving it alone", _dialog_name(dialog))
            return
        name = _dialog_name(dialog)
        self._start_hold()
        try:
            # The dialog's own exit, which is what EXIT runs: the channel list
            # puts back the service the user was on before closing. A raw
            # `close()` would leave somebody who was previewing channels on
            # whichever one the cursor happened to be over.
            cancel = getattr(dialog, "cancel", None)
            if callable(cancel):
                cancel()
            else:
                dialog.close()
        except Exception:
            # Most likely the user closed it in the same instant. That is the
            # outcome that was wanted anyway.
            LOG.exception("closing %s raised; the receiver is unaffected", name)
            return
        self._count += 1
        self._kind = KIND_CLOSED
        self._last_intervention = int(time.time())
        LOG.info("closed %s so that the television's standby could proceed", name)
        self._publish()

    # ----------------------------------------------------------- holding the echo --

    def _start_hold(self):
        """Hold `handlingStandbyFromTV` asserted across the close. See the module."""
        if self._hold is not None:
            # Already held for an earlier close: the value to put back is the one
            # recorded then, not the `True` this module wrote.
            self._hold_deadline.start(HOLD_SECONDS * 1000, True)
            return
        try:
            instance = self._cec_class.instance
            if instance is None:
                LOG.info("HDMI-CEC is no longer reachable; the list is closed anyway, and the "
                         "receiver may echo the standby back to the television")
                return
            previous = instance.handlingStandbyFromTV
            instance.handlingStandbyFromTV = True
        except Exception:
            LOG.exception("could not hold the television's standby flag; the list is closed "
                          "anyway, and the receiver may echo the standby back")
            return
        self._hold = (instance, previous)
        self._hold_deadline.start(HOLD_SECONDS * 1000, True)

    def _release_hold(self):
        held, self._hold = self._hold, None
        self._hold_deadline.stop()
        if held is None:
            return
        instance, previous = held
        try:
            instance.handlingStandbyFromTV = previous
        except Exception:
            LOG.exception("could not put the television's standby flag back")

    def _counter_moved(self, _element=None):
        """The receiver entered standby. Look again once the image has read the flag.

        🔴 Never restore from in here: the image's own notifier on this counter
        may not have run yet. See `SETTLE_MILLISECONDS`.
        """
        try:
            if self._hold is not None or self._queued:
                self._settle.start(SETTLE_MILLISECONDS, True)
        except Exception:
            LOG.exception("the standby counter hook raised; the receiver is unaffected")

    def _after_standby(self):
        self._release_hold()
        for queued in list(self._queued):
            if not self._still_queued(queued.entry):
                self._forget(queued)
        self._publish()

    # ------------------------------------------------------------ part 2: stale --

    def _stale(self, entry):
        """Part 2: an identified standby nobody drained in `STALE_SECONDS`."""
        queued = next((item for item in self._queued if item.entry is entry), None)
        if queued is None:
            return
        self._forget(queued)
        removed = False
        try:
            queue = self._notifications.notifications
            for index, item in enumerate(queue):
                # By identity and nothing else: the household's own standby is
                # an equal tuple, and must survive this.
                if item is entry:
                    del queue[index]
                    removed = True
                    break
        except Exception:
            LOG.exception("could not remove a stale standby from the queue")
        if removed:
            self._count += 1
            self._kind = KIND_DROPPED
            self._last_intervention = int(time.time())
            LOG.info("dropped a standby the television asked for %d seconds ago and nothing "
                     "carried out", STALE_SECONDS)
        else:
            LOG.debug("a standby from the television was carried out normally")
        self._publish()

    def _forget(self, queued):
        queued.deadline.stop()
        if queued in self._queued:
            self._queued.remove(queued)

    # --------------------------------------------------------------- publishing --

    def _payload(self):
        return {
            "last_intervention": self._last_intervention,
            "kind": self._kind,
            "count": self._count,
            "pending": bool(self._queued),
        }

    def _publish(self):
        if not self._bound:
            return
        self.publish(TOPIC, self._payload())

    def snapshot(self):
        return {TOPIC: self._payload()}
