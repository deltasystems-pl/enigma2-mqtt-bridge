"""The popup on the television — `cmd/message`, and its default style.

The other style, `toast`, is `toast.py`: a screen of the plugin's own rather than
the image's message box.

The text is not translated here. It arrives from whatever sent the command,
already in the language of the household that sent it; a plugin that ran it
through its own catalogue would mangle a sentence it has never seen. What *is*
this plugin's business is that the popup cannot stack: every message replaces
the previous one, because `AddPopup` with no identifier would leave a queue of
notifications for somebody to dismiss one at a time with a remote control.

🔴 **No backslash reaches the screen here either.** The popup's text is drawn
by the same enigma2 text renderer as the toast's, which reads a backslash and
what follows it as a colour change or a line break, and reads it after
right-to-left reordering, where no pattern over the string can find it. So the
popup follows the toast's rule, through the toast's own function, so that there
is one rule in one place: every backslash is removed and nothing else. A literal
`\\n` shows as `n`, a real newline stays a line break, and the 500 characters
are counted after the removal, so they are 500 displayed ones. A text that was
nothing but backslashes and blanks is refused as empty, exactly as a blank one
always was. The reasoning, and the measurement behind it, is in `toast.py`.
"""

from .log import get_logger

LOG = get_logger("osd")

# The identifier that makes a new popup replace the old one instead of queueing.
POPUP_ID = "mqttbridge"

MAX_TEXT = 500
DEFAULT_TIMEOUT = 10

# The contract's names for what enigma2 calls TYPE_INFO, TYPE_WARNING, TYPE_ERROR.
TYPES = ("info", "warning", "error")


def _notifications():
    try:
        import Tools.Notifications as notifications

        return notifications
    except Exception:
        return None


def _message_box():
    try:
        from Screens.MessageBox import MessageBox

        return MessageBox
    except Exception:
        return None


def popups_available():
    """Whether this image can show a popup at all — the `message` capability."""
    notifications = _notifications()
    return (
        notifications is not None
        and getattr(notifications, "AddPopup", None) is not None
        and _message_box() is not None
    )


def box_type(kind):
    """The contract's `info`/`warning`/`error` as enigma2's own constant."""
    message_box = _message_box()
    if message_box is None:
        return None
    attribute = {
        "info": "TYPE_INFO",
        "warning": "TYPE_WARNING",
        "error": "TYPE_ERROR",
    }.get(kind, "TYPE_INFO")
    return getattr(message_box, attribute, getattr(message_box, "TYPE_INFO", 1))


def type_refusal(kind):
    """Why `kind` is not a message type, or None when it is.

    Shared with the toast, which validates `type` exactly as the popup does and
    then ignores it, so that a payload is valid or invalid whatever its style.
    """
    if kind not in TYPES:
        return "unknown message type '" + str(kind) + "'; expected one of " + ", ".join(TYPES)
    return None


def show(text, kind="info", timeout=DEFAULT_TIMEOUT):
    """Put `text` on the screen. None on success, otherwise the refusal."""
    # Imported here rather than at the top: `toast` reaches back into this
    # module for the type check, and neither needs the other to import.
    from .toast import strip_backslashes

    text = strip_backslashes(str(text or ""))
    if not text.strip():
        return "message text is empty"
    if len(text) > MAX_TEXT:
        LOG.info("message truncated from %d to %d characters", len(text), MAX_TEXT)
        text = text[:MAX_TEXT]

    refusal = type_refusal(kind)
    if refusal:
        return refusal

    notifications = _notifications()
    if notifications is None or getattr(notifications, "AddPopup", None) is None:
        return "this image has no Tools.Notifications.AddPopup"

    remove = getattr(notifications, "RemovePopup", None)
    if remove is not None:
        try:
            remove(POPUP_ID)
        except Exception:
            # An identifier that was never shown is not an error worth refusing
            # the new popup over.
            LOG.debug("no earlier popup to remove")

    try:
        notifications.AddPopup(text, box_type(kind), int(timeout), id=POPUP_ID)
    except Exception as error:
        LOG.exception("the popup could not be shown")
        return type(error).__name__ + ": " + str(error)
    return None
