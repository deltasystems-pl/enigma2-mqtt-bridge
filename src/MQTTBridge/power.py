"""Standby, and the three ways of leaving the room.

The power model is the household's, not the hardware's. `on` and `standby` are
the two states the box has while it is still doing its job - recordings run, the
disk stays mounted, this plugin stays connected - and deep standby is not a
third state but the absence of one: the box is off, the session is gone, and
`availability` says `offline`. A consumer infers "off" from availability rather
than from a `power` payload that could never be published.

Standby is watched from both ends because enigma2 offers no single event for it.
`config.misc.standbyCounter` increments when the box *enters* standby; leaving it
is the standby screen closing, which is only observable by attaching to that
screen's `onClose` while it exists. So the counter's notifier does two things:
publishes `standby`, and books the listener that will publish `on` later.
"""

from .enigma2 import Ticker, missing
from .log import get_logger
from .publisher import Publisher

LOG = get_logger("power")

ON = "on"
STANDBY = "standby"

# enigma2's own numbering for TryQuitMainloop. Read off the class where the
# image provides the names, because these are the values that turn a box off.
QUIT_SHUTDOWN = 1
QUIT_REBOOT = 2
QUIT_RESTART = 3


def standby_module():
    try:
        import Screens.Standby as standby

        return standby
    except Exception as error:
        missing("Screens.Standby", error)
        return None


def in_standby():
    """True, False, or None when this image will not say."""
    module = standby_module()
    if module is None:
        return None
    return getattr(module, "inStandby", None) is not None


def state():
    """The `power` payload: `on` or `standby`."""
    standby = in_standby()
    return STANDBY if standby else ON


def wake():
    """Leave standby. None on success, otherwise the reason it could not.

    `inStandby.Power()` is the standby screen's own handler for the power
    button, so waking this way is exactly what the remote does - including the
    parts of it a plugin has no business reimplementing, like restoring the
    service that was playing.
    """
    module = standby_module()
    if module is None:
        return "this image has no Screens.Standby"
    screen = getattr(module, "inStandby", None)
    if screen is None:
        return None  # already awake; the command is idempotent
    try:
        screen.Power()
    except Exception as error:
        LOG.exception("could not leave standby")
        return type(error).__name__ + ": " + str(error)
    return None


def enter_standby():
    """Go to standby through the notification queue.

    The remote's power button does not queue anything: `StartEnigma`'s
    `PowerKey.standby` opens `Standby` directly with `session.open` - but only
    when the executing dialog allows suspend and the session is executing, and
    otherwise does nothing. A command arrives whenever the broker delivers it,
    with any screen open, so the plugin cannot make that check once and open the
    screen itself: a screen opened on top of whatever is already open is how a
    receiver ends up with a standby screen behind a dialogue. `AddNotification`
    hands it to enigma2, which opens it when the session is in a state to take
    it.
    """
    module = standby_module()
    if module is None:
        return "this image has no Screens.Standby"
    if getattr(module, "inStandby", None) is not None:
        return None  # already there; the command is idempotent
    screen = getattr(module, "Standby", None)
    if screen is None:
        return "this image has no Screens.Standby.Standby"
    try:
        from Tools.Notifications import AddNotification
    except Exception as error:
        missing("Tools.Notifications.AddNotification", error)
        return "this image has no Tools.Notifications"
    try:
        AddNotification(screen)
    except Exception as error:
        LOG.exception("could not enter standby")
        return type(error).__name__ + ": " + str(error)
    return None


def can_quit(session):
    """Why the box could not be shut down at all, or None when it could.

    Asked *before* the plugin says goodbye to the broker. Disconnecting and
    then discovering that this image has no `TryQuitMainloop` would leave a
    receiver that is running, working, and marked offline.
    """
    module = standby_module()
    if module is None:
        return "this image has no Screens.Standby"
    if getattr(module, "TryQuitMainloop", None) is None:
        return "this image has no TryQuitMainloop"
    if session is None:
        return "there is no session to open the shutdown screen in"
    return None


def quit_mainloop(session, retvalue):
    """Shut down (1), reboot (2) or restart the user interface (3).

    Every caller has passed the recording guard before reaching this; the guard
    is not here because it belongs to the command, which has a `last_error` to
    refuse into.
    """
    module = standby_module()
    if module is None:
        return "this image has no Screens.Standby"
    screen = getattr(module, "TryQuitMainloop", None)
    if screen is None:
        return "this image has no TryQuitMainloop"
    if session is None:
        return "there is no session to open the shutdown screen in"
    try:
        session.open(screen, retvalue)
    except Exception as error:
        LOG.exception("TryQuitMainloop(%s) raised", retvalue)
        return type(error).__name__ + ": " + str(error)
    return None


class PowerPublisher(Publisher):
    """`power` - `on` or `standby`, and not JSON."""

    name = "power"
    raw = ("power",)

    # enigma2 tears the standby screen down after `onClose` fires, and
    # `inStandby` is cleared as part of that. Reading it in the same call stack
    # can therefore see the screen that is on its way out, so the publish is
    # deferred by one turn of the main loop.
    SETTLE_MILLISECONDS = 100

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._counter = None
        self._attached_to = None
        self._settle = Ticker(self._publish_now, "standby settle")

    # ------------------------------------------------------------------ hooks --

    def start(self):
        if standby_module() is None:
            return False
        counter = self._standby_counter()
        if counter is None:
            return False
        notifier = getattr(counter, "addNotifier", None)
        if notifier is None:
            missing("config.misc.standbyCounter.addNotifier")
            return False
        try:
            # `initial_call=False`: enigma2 would otherwise call the notifier
            # during `addNotifier` itself, which on some images happens before
            # the session is usable.
            notifier(self._entered_standby, initial_call=False)
        except TypeError:
            # An older signature with no keyword. The extra call it makes is
            # harmless: it publishes the state we are about to publish anyway.
            try:
                notifier(self._entered_standby)
            except Exception as error:
                missing("config.misc.standbyCounter.addNotifier", error)
                return False
        except Exception as error:
            missing("config.misc.standbyCounter.addNotifier", error)
            return False
        self._counter = counter
        self._attach_to_standby_screen()
        return True

    def stop(self):
        self._settle.stop()
        counter = self._counter
        self._counter = None
        if counter is None:
            return
        remover = getattr(counter, "removeNotifier", None)
        try:
            if remover is not None:
                remover(self._entered_standby)
            else:
                notifiers = getattr(counter, "notifiers", None)
                if notifiers is not None and self._entered_standby in notifiers:
                    notifiers.remove(self._entered_standby)
        except Exception:
            LOG.debug("could not detach from the standby counter")
        self._detach_from_standby_screen()

    def _standby_counter(self):
        try:
            from Components.config import config
        except Exception as error:
            missing("Components.config", error)
            return None
        misc = getattr(config, "misc", None)
        counter = getattr(misc, "standbyCounter", None) if misc is not None else None
        if counter is None:
            missing("config.misc.standbyCounter")
        return counter

    # ------------------------------------------------------------- transitions --

    def _entered_standby(self, _element=None):
        self._attach_to_standby_screen()
        self._publish_now()

    def _left_standby(self):
        self._attached_to = None
        # `onClose` fires while the screen is still the current one.
        self._settle.start(self.SETTLE_MILLISECONDS, True)

    def _attach_to_standby_screen(self):
        module = standby_module()
        screen = getattr(module, "inStandby", None) if module is not None else None
        if screen is None or screen is self._attached_to:
            return
        hook = getattr(screen, "onClose", None)
        if hook is None:
            missing("Screens.Standby.inStandby.onClose")
            return
        try:
            hook.append(self._left_standby)
            self._attached_to = screen
        except Exception as error:
            missing("Screens.Standby.inStandby.onClose", error)

    def _detach_from_standby_screen(self):
        screen, self._attached_to = self._attached_to, None
        if screen is None:
            return
        try:
            hook = getattr(screen, "onClose", None)
            if hook is not None and self._left_standby in hook:
                hook.remove(self._left_standby)
        except Exception:
            LOG.debug("could not detach from the standby screen")

    # --------------------------------------------------------------- publishing --

    def _publish_now(self):
        self.publish("power", state())

    def snapshot(self):
        return {"power": state()}
