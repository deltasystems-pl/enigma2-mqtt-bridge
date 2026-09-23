"""The discreet toast — `cmd/message` with `"style": "toast"`.

**Why not the popup.** The popup is the image's own `MessageBox`, opened through
the notification queue. It takes focus, and the queue is drained only while the
info bar is the screen executing, so a message sent while the channel list is
open waits behind the list and then interrupts whoever closes it. `MessageBox`
cannot be borrowed non-modally either: its own timeout calls `close()`, which
does nothing on a screen that is not executing, and it looks like a system
dialog, which a message from a broker must never do.

**Why a dialog that is never executed.** The image already puts transient
overlays on the screen this way — the volume bar and the unhandled-key symbol are
both created with `session.instantiateDialog()`, shown with `show()`, and hidden
by an `eTimer`. `instantiateDialog` builds, skins and lays the screen out and
never touches the dialog stack, `current_dialog` or `in_exec`. A Python action
map binds only between `execBegin` and `execEnd`, and `show()` calls neither. So
the toast is instantiated once, driven by `show()`/`hide()` and one single-shot
timer, and is never passed to `open`, `openWithCallback` or `execDialog`.

🔴 **The widget rule — the property that makes „cannot steal a key press" true.**
„No action map is bound" is not enough on its own. Every key reaches the
interface through one dispatcher, ordered by priority and, within a priority, by
the age of the binding, and it holds two kinds of binding: Python action maps,
which a never-executed dialog does not bind, and **native widget bindings, which
need neither an action map nor exec**. The list widget, `eListbox`, binds the
list-navigation keys at priority 0 **in its own constructor**, and while it is
visible it consumes UP/DOWN/LEFT/RIGHT, the channel keys and the skip keys and
stops dispatch. The channel list moves on exactly those bindings. A toast holding
any list-type widget, created at session start and therefore older than the
channel list, would be asked first and eat the DOWN key while it is on screen;
after a skin reload the order reverses and the bug hides. So the screen holds
**two labels and nothing else**: never a `MenuList`, a `List`, a `ChoiceList`, a
`ConfigList`, a `SelectionList`, a `ScrollLabel` or an `Input`, each of which is
built on a widget that binds keys without being executed. A test enforces it.

**Z-position 10.** Screens are stacked by z; within one z the *older* window is
in front. The channel list, the info bar, every message box and the standby
screen are at 0 on the skins read, the image's own transient overlays (volume,
mute, the unhandled-key symbol) at 10. The toast has to be strictly above the
channel list without depending on which was created first — a skin reload
recreates it and would put it behind — and 10 is the tier the image already uses
for exactly this kind of window.

**Geometry is computed, not written into the skin.** Numbers in a plugin's
embedded skin are desktop pixels: the image scales them by the identity. So the
toast reads the desktop's size, takes `f = height / 720` (the image's own skin
factor), and formats plain integers into the skin string before
`Screen.__init__`, per instance — the way the image's own shutdown screen builds
its skin. It depends on none of the skin-expression features (`f`, `e`, list
templates), which not every image has. Top right: a fixed width, equal margins
from the top and the right edge, and a height that follows the text, capped.

**One fixed appearance.** A dark, slightly transparent box and light text, the
same for every message, under a header that always reads „MQTT Bridge" in the
receiver's language. The header is not the device name, which is provisioned from
outside and could be anything; it is what says a message came from this plugin
rather than from the receiver. The `type` a payload carries is validated as for a
popup and then ignored, and a colour escape in the text is removed, so nothing in
a payload can dress a toast up as something else. A skin may define a screen
called `MQTTBridgeToast` and restyle it, as it may any screen: that is the box
owner's choice, not a broker client's.

**Lifecycle.** Instantiated when the bridge starts with a session and `osd_toast`
on, and the capability `toast` is claimed only once that has worked — an image
where it fails keeps popups rather than gaining a style that silently does
nothing. 🔴 Torn down with `session.deleteDialog()`, never `close()`: on a dialog
that is not executing `close()` only records a value for an `execBegin` that will
never come and hides nothing — the image's own volume control leaks its windows
on every skin reload exactly that way. The timer is stopped *before* the delete,
because the delete sets every attribute of the screen to `None`. The bridge stops
its publishers on every path that stops it — a settings save, removal from the
plugin browser, shutdown — and all of them need the delete: enigma2 repaints the
desktop once more after the plugins are shut down, so a toast still showing then
is the frame a restarting receiver leaves on the television.

**Standby.** The standby screen is a full-screen black window at z 0, so a toast
is drawn *over* it on a television that is still on. The toast is hidden when the
receiver enters standby, and a toast asked for while it is in standby — or on
its way out of the main loop — is refused rather than kept for later: a late
toast is a wrong toast, and a refusal on `last_error` is something a sender can
see.

Everything here runs on the main loop — commands already arrive there, and
nothing blocks — and every entry point is wrapped: a toast that fails is a line
in the log, never a traceback in the middle of the interface.
"""

import re

from .enigma2 import Ticker, enigma_attribute, missing
from .i18n import _
from .log import get_logger
from .publisher import Publisher

LOG = get_logger("toast")

# The capability, and the publisher's name.
CAPABILITY = "toast"

# What a skin calls the screen if it wants to restyle it.
SKIN_NAME = "MQTTBridgeToast"

# The tier the image uses for its own transient overlays. See the module.
Z_POSITION = 10

MAX_TEXT = 200
DEFAULT_TIMEOUT = 5
MIN_TIMEOUT = 1
MAX_TIMEOUT = 30

# The shape at a 720-line desktop; everything is multiplied by `height / 720`.
# Width, margin, fonts and the height cap are the specification's; the padding,
# header height and gap are this module's. All of them are a starting point for
# a look at a real television, not a measurement.
BASE_LINES = 720
BASE_WIDTH = 420
BASE_MARGIN = 12
BASE_HEADER_FONT = 16
BASE_MESSAGE_FONT = 20
BASE_MAX_HEIGHT = 220
BASE_PADDING = 8
BASE_HEADER_HEIGHT = 22
BASE_GAP = 4

# enigma2 colours are #AARRGGBB, and an alpha of 00 is opaque.
BACKGROUND = "#30101418"
HEADER_COLOUR = "#00b8c0c8"
TEXT_COLOUR = "#00f0f0f0"

# enigma2's text renderer reads a backslash, a `c` and the eight characters after
# them as a colour change. A toast whose colour the payload chooses is not one
# fixed appearance.
COLOUR_ESCAPE = re.compile(r"\\c.{8}", re.DOTALL)

SWITCHED_OFF = "the discreet toast is switched off on this receiver"
NOT_CREATED = "the discreet toast could not be created on this receiver"
IN_STANDBY = "the receiver is in standby"
BAD_TIMEOUT = "a toast hides itself; timeout must be 1–30 seconds"


# ---------------------------------------------------------------- the text --


def strip_colour_escapes(text):
    """`text` without any colour escape — including one a removal would create.

    Removing `\\cXXXXXXXX` from between a backslash and a `c` joins them into a
    new escape, so this repeats until nothing changes.
    """
    previous = None
    while previous != text:
        previous = text
        text = COLOUR_ESCAPE.sub("", text)
    return text


def prepare_text(text):
    """What the toast will display, or None when nothing would be left of it."""
    text = strip_colour_escapes(str(text or ""))
    if not text.strip():
        return None
    if len(text) > MAX_TEXT:
        LOG.info("toast truncated from %d to %d characters", len(text), MAX_TEXT)
        text = text[:MAX_TEXT]
    return text


# ------------------------------------------------------------ the geometry --


def geometry(desktop_width, desktop_height):
    """Every number the skin and the resizing need, as plain desktop pixels."""
    factor = float(desktop_height) / BASE_LINES
    margin = int(BASE_MARGIN * factor)
    # Never wider than the desktop, whatever shape it is.
    width = max(1, min(int(BASE_WIDTH * factor), int(desktop_width) - 2 * margin))
    padding = int(BASE_PADDING * factor)
    header_height = int(BASE_HEADER_HEIGHT * factor)
    gap = int(BASE_GAP * factor)
    max_height = int(BASE_MAX_HEIGHT * factor)
    chrome = 2 * padding + header_height + gap
    return {
        "x": int(desktop_width) - width - margin,
        "y": margin,
        "width": width,
        "margin": margin,
        "padding": padding,
        "header_height": header_height,
        "gap": gap,
        "header_font": int(BASE_HEADER_FONT * factor),
        "message_font": int(BASE_MESSAGE_FONT * factor),
        "max_height": max_height,
        "text_width": max(1, width - 2 * padding),
        "message_y": padding + header_height + gap,
        "chrome": chrome,
        "message_max": max(1, max_height - chrome),
    }


def build_skin(shape):
    """The embedded skin, with every number already a plain integer."""
    return (
        '<screen name="{name}" position="{x},{y}" size="{width},{max_height}" '
        'zPosition="{z}" flags="wfNoBorder" backgroundColor="{background}">'
        '<widget name="header" position="{padding},{padding}" '
        'size="{text_width},{header_height}" font="Regular;{header_font}" '
        'foregroundColor="{header_colour}" backgroundColor="{background}" transparent="1" />'
        '<widget name="message" position="{padding},{message_y}" '
        'size="{text_width},{message_max}" font="Regular;{message_font}" '
        'foregroundColor="{text_colour}" backgroundColor="{background}" transparent="1" />'
        "</screen>"
    ).format(
        name=SKIN_NAME,
        z=Z_POSITION,
        background=BACKGROUND,
        header_colour=HEADER_COLOUR,
        text_colour=TEXT_COLOUR,
        **shape
    )


def desktop_geometry():
    """The shape for the desktop as it is now, or None when the image will not say."""
    get_desktop = enigma_attribute("getDesktop")
    if get_desktop is None:
        return None
    size = get_desktop(0).size()
    return geometry(size.width(), size.height())


# -------------------------------------------------------------- the screen --

_screen_class = None


def screen_class():
    """The toast's `Screen` subclass, built on first use.

    Built lazily so that importing this module needs nothing from the image, and
    named `MQTTBridgeToast` because a screen's class name is its skin name.
    """
    global _screen_class
    if _screen_class is not None:
        return _screen_class

    from Components.Label import Label
    from Screens.Screen import Screen

    from .plugin import PLUGIN_NAME

    class MQTTBridgeToast(Screen):
        """Two labels in a box, top right. 🔴 Nothing else — see the widget rule."""

        def __init__(self, session, shape):
            # Before `Screen.__init__`, as the image's own shutdown screen does:
            # the skin is read from the instance, and these numbers are this
            # desktop's.
            self.skin = build_skin(shape)
            Screen.__init__(self, session)
            self.shape = shape
            self["header"] = Label(_(PLUGIN_NAME))
            self["message"] = Label("")

    _screen_class = MQTTBridgeToast
    return _screen_class


def _layout(dialog):
    """The box as it was laid out, read back from its widgets.

    Read back rather than taken from `geometry()`, because a skin that restyles
    `MQTTBridgeToast` decides the width and the room for the text, and fitting
    the height has to work inside the box the skin drew, not the one this
    module would have.
    """
    window = dialog.instance.size()
    label = dialog["message"].instance.size()
    return {
        "width": window.width(),
        "text_width": label.width(),
        "message_max": max(1, label.height()),
        "chrome": max(0, window.height() - label.height()),
    }


def _receiver_in_standby():
    """In standby, or on the way out of the main loop. False when the image will not say."""
    try:
        import Screens.Standby as standby
    except Exception as error:
        missing("Screens.Standby", error)
        return False
    return (
        getattr(standby, "inStandby", None) is not None
        or bool(getattr(standby, "inTryQuitMainloop", False))
    )


# ----------------------------------------------------------- the publisher --


class ToastPublisher(Publisher):
    """The toast screen's owner. It publishes nothing; it is a capability and a lifecycle.

    A publisher because the registry already gives it exactly what it needs: it
    is started only with a session, it names itself in `capabilities` only when
    it worked, and it is stopped on every path that stops the bridge.
    """

    name = CAPABILITY

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._dialog = None
        self._layout = None
        self._counter = None
        self._skin_hooked = False
        self._timer = Ticker(self._expire, "toast")

    # -------------------------------------------------------------- lifecycle --

    def start(self):
        if not self.value("osd_toast"):
            self.switched_off = True
            LOG.info("osd_toast is off; the discreet toast is not created")
            return False
        if self.session is None:
            return False
        if not self._instantiate():
            return False
        self._counter = self._bind_counter()
        self._hook_skin_reload()
        LOG.info("the discreet toast is ready")
        return True

    def claimed(self):
        return self._dialog is not None

    def stop(self):
        try:
            self._unhook_skin_reload()
            self._unbind_counter()
            self._delete()
        except Exception:
            LOG.exception("letting go of the discreet toast raised; the receiver is unaffected")

    def _instantiate(self):
        """Create the screen. True when it exists; one log line when it does not."""
        session = self.session
        factory = getattr(session, "instantiateDialog", None)
        if factory is None:
            missing("session.instantiateDialog")
            return False
        try:
            shape = desktop_geometry()
            if shape is None:
                return False
            dialog = factory(screen_class(), shape)
        except Exception:
            LOG.exception("the discreet toast could not be created; messages stay popups")
            return False
        if dialog is None:
            LOG.warning("the image returned no toast screen; messages stay popups")
            return False
        try:
            # As the image does for its own overlays, so the toast appears at
            # once rather than sliding in. Not every image has it.
            setter = getattr(dialog, "setAnimationMode", None)
            if setter is not None:
                setter(0)
        except Exception:
            LOG.debug("this image's toast screen took no animation mode")
        try:
            layout = _layout(dialog)
        except Exception:
            LOG.exception("the discreet toast has no usable layout; messages stay popups")
            self._dialog = dialog
            self._delete()
            return False
        self._dialog = dialog
        self._layout = layout
        return True

    def _delete(self):
        """🔴 Timer first, then `deleteDialog`, then the reference. Never `close()`."""
        self._timer.stop()
        dialog = self._dialog
        if dialog is None:
            return
        try:
            delete = getattr(self.session, "deleteDialog", None)
            if delete is not None:
                delete(dialog)
            else:
                missing("session.deleteDialog")
                dialog.hide()
        except Exception:
            LOG.exception("the discreet toast could not be deleted")
        finally:
            self._dialog = None

    # --------------------------------------------------------------- showing --

    def show(self, text, timeout):
        """Put `text` up for `timeout` seconds. None on success, otherwise the refusal.

        The newest message replaces the one on screen and restarts the timer:
        a toast that queued would be a popup with extra steps.
        """
        dialog = self._dialog
        if dialog is None:
            return NOT_CREATED
        if _receiver_in_standby():
            return IN_STANDBY
        try:
            self._timer.stop()
            dialog["message"].setText(text)
            self._fit(dialog)
            dialog.show()
            self._timer.start(int(timeout) * 1000, True)
        except Exception as error:
            LOG.exception("the discreet toast could not be shown")
            return type(error).__name__ + ": " + str(error)
        return None

    def _fit(self, dialog):
        """Make the box as tall as the text, and no taller than the cap."""
        layout = self._layout
        message = dialog["message"]
        # Measured against the tallest box it may have, so the text wraps at the
        # real width however short the previous message was.
        message.resize(layout["text_width"], layout["message_max"])
        height = message.getSize()[1]
        height = max(1, min(int(height), layout["message_max"]))
        message.resize(layout["text_width"], height)
        size = enigma_attribute("eSize")
        if size is not None:
            dialog.instance.resize(size(layout["width"], layout["chrome"] + height))

    def _expire(self):
        dialog = self._dialog
        if dialog is not None:
            dialog.hide()

    # ---------------------------------------------------------------- standby --

    def _bind_counter(self):
        try:
            from Components.config import config

            counter = config.misc.standbyCounter
            notifier = counter.addNotifier
        except Exception as error:
            missing("config.misc.standbyCounter.addNotifier", error)
            return None
        try:
            notifier(self._entered_standby, initial_call=False)
        except TypeError:
            try:
                notifier(self._entered_standby)
            except Exception as error:
                missing("config.misc.standbyCounter.addNotifier", error)
                return None
        except Exception as error:
            missing("config.misc.standbyCounter.addNotifier", error)
            return None
        return counter

    def _unbind_counter(self):
        counter, self._counter = self._counter, None
        if counter is None:
            return
        try:
            remover = getattr(counter, "removeNotifier", None)
            if remover is not None:
                remover(self._entered_standby)
            elif self._entered_standby in getattr(counter, "notifiers", []):
                counter.notifiers.remove(self._entered_standby)
        except Exception:
            LOG.debug("could not detach from the standby counter")

    def _entered_standby(self, _element=None):
        """The receiver went to standby: nothing may stay drawn over its black screen."""
        try:
            self._timer.stop()
            dialog = self._dialog
            if dialog is not None:
                dialog.hide()
        except Exception:
            LOG.exception("hiding the discreet toast for standby raised")

    # ------------------------------------------------------------ skin reload --

    def _hook_skin_reload(self):
        try:
            import skin

            skin.addOnLoadCallback(self._skin_reloaded)
        except Exception as error:
            missing("skin.addOnLoadCallback", error)
            return
        self._skin_hooked = True

    def _unhook_skin_reload(self):
        if not self._skin_hooked:
            return
        self._skin_hooked = False
        try:
            import skin

            skin.removeOnLoadCallback(self._skin_reloaded)
        except Exception:
            LOG.debug("could not detach from the skin reload")

    def _skin_reloaded(self):
        """A new skin, and possibly a new desktop size: build the toast again."""
        try:
            before = self.claimed()
            self._delete()
            if not self._instantiate():
                LOG.warning("the discreet toast could not be rebuilt after a skin reload")
            if self.claimed() != before and self.bridge is not None:
                self.bridge.announce_capabilities()
        except Exception:
            LOG.exception("rebuilding the discreet toast after a skin reload raised")


# ------------------------------------------------------------- the command --


def request(bridge, text, kind, timeout):
    """`cmd/message` with `style: toast`, after the timeout is a number. None or a refusal.

    Validated first, and in the popup's order, so that a payload is valid or
    invalid whatever its style; only then is the toast asked whether it can.
    """
    from . import osd

    body = prepare_text(text)
    if body is None:
        return "message text is empty"
    refusal = osd.type_refusal(kind)
    if refusal:
        return refusal
    if timeout < MIN_TIMEOUT:
        return BAD_TIMEOUT
    if timeout > MAX_TIMEOUT:
        LOG.info("toast timeout %d clamped to %d seconds", timeout, MAX_TIMEOUT)
        timeout = MAX_TIMEOUT

    publisher = bridge.publisher(CAPABILITY)
    if publisher is None or not publisher.claimed():
        return NOT_CREATED if bridge.value("osd_toast") else SWITCHED_OFF
    return publisher.show(body, timeout)
