"""PREVIOUS works as EXIT on the plugin's own screens.

A television's remote over HDMI-CEC sends its Back key as `KEY_PREVIOUS` on many televisions
(`combo.py`). On OpenViX that key is no "cancel": `OkCancelActions` maps only EXIT, and
`DirectionActions` maps `KEY_PREVIOUS` to `shiftUp`. A household that opened the receiver menu
with three presses of Back could then not leave it. So on the screens this plugin opens - the
receiver menu and the list of choices, the plugin's own styled screens and the image's
`ChoiceBox` in their role - `KEY_PREVIOUS` does what EXIT does there: back one level, or close.
That holds whatever `menu_combo_key` says. The image's own screens are left alone, the
keyboard a menu row opens among them.

How:

- While any such screen is open, `KEY_PREVIOUS` is mapped at run time, on its make only, into a
  context of its own, `MQTTBridgeBackActions`, under a keymap domain of its own; the last screen
  to close takes the mapping back.
- Each screen gets a binding of its own, made when the screen is opened and let go of when it
  closes, at `PRIORITY`: after the press combo's and before every binding of the image. It
  answers only while its screen is the executing dialog, so a keyboard opened over the menu
  keeps the key.
- **The press that opened a screen never reaches it.** The binding is made while that press is
  being handed out, and enigma2's dispatcher only hands the rest of a press - its repeats and
  its break - to a binding that saw its make. Only the make is mapped in any case. So the menu
  opened by the third Back of a press combo stays open.
"""

import sys

from .enigma2 import enigma_attribute, missing
from .keys import code_for
from .log import get_logger

LOG = get_logger("backkey")

CONTEXT = "MQTTBridgeBackActions"
DOMAIN = "MQTTBridge-back-key"
DEVICE = "generic"
ACTION = "back"
KEY = "KEY_PREVIOUS"
FALLBACK_CODE = 412
FLAG_MAKE = 0
# After the press combo (`combo.PRIORITY`, `-sys.maxsize`), before every binding of the image.
PRIORITY = -sys.maxsize + 1

# How many open screens hold the mapping, and the action map they hold it in: one mapping for
# all of them, since two identical mappings would hand one press to a binding twice.
_mapped = {"actions": None, "count": 0}


def _action_map():
    factory = enigma_attribute("eActionMap")
    if factory is None:
        return None
    try:
        return factory.getInstance()
    except Exception:
        LOG.exception("eActionMap.getInstance() raised")
        return None


def _map(actions):
    """Map the key while at least one screen is open. False when the image cannot."""
    bind_key = getattr(actions, "bindKey", None)
    if bind_key is None:
        missing("eActionMap.bindKey")
        return False
    if _mapped["actions"] is not actions or not _mapped["count"]:
        try:
            bind_key(DOMAIN, DEVICE, int(code_for(KEY) or FALLBACK_CODE), 1 << FLAG_MAKE,
                     CONTEXT, ACTION)
        except Exception as error:
            missing("eActionMap.bindKey", error)
            return False
        _mapped["actions"] = actions
        _mapped["count"] = 0
    _mapped["count"] += 1
    return True


def _unmap(actions):
    if actions is None or _mapped["actions"] is not actions:
        return
    _mapped["count"] = max(0, _mapped["count"] - 1)
    if _mapped["count"]:
        return
    _mapped["actions"] = None
    unbind_domain = getattr(actions, "unbindKeyDomain", None)
    if unbind_domain is None:
        return
    try:
        unbind_domain(DOMAIN)
    except Exception:
        LOG.debug("could not take the PREVIOUS key's mapping back")


class BackKey:
    """PREVIOUS as EXIT over one dialog, from its opening to its close."""

    def __init__(self, session, dialog, cancel):
        self._session = session
        self._dialog = dialog
        self._cancel = cancel
        # One object for the life of the binding: the action map compares by identity when
        # unbinding (see `remote.py`).
        self._handler = self._action
        self.bound = False

    def bind(self):
        actions = _action_map()
        if actions is None or getattr(actions, "bindAction", None) is None:
            return False
        if not _map(actions):
            return False
        try:
            actions.bindAction(CONTEXT, PRIORITY, self._handler)
        except Exception as error:
            missing("eActionMap.bindAction", error)
            _unmap(actions)
            return False
        self.bound = True
        return True

    def unbind(self):
        if not self.bound:
            return
        self.bound = False
        actions = _action_map()
        if actions is not None:
            try:
                actions.unbindAction(CONTEXT, self._handler)
            except Exception:
                LOG.debug("could not unbind the PREVIOUS key from a closed screen")
        _unmap(actions)

    def _action(self, context, action):
        """1 when PREVIOUS was EXIT on the executing screen. Never raises."""
        try:
            if context != CONTEXT or action != ACTION or not self.bound:
                return 0
            if getattr(self._session, "current_dialog", None) is not self._dialog:
                return 0
            self._cancel()
            return 1
        except Exception:
            LOG.exception("PREVIOUS as EXIT raised; the key goes on to the image")
            return 0


def attach(session, dialog, cancel):
    """Give `dialog` - a screen the plugin opened - PREVIOUS as EXIT until it closes.

    `cancel` is what EXIT does on it. Returns the binding, or None when the image cannot
    bind a key; the screen works as before without it.
    """
    if dialog is None or not callable(cancel):
        return None
    back = BackKey(session, dialog, cancel)
    if not back.bind():
        return None
    try:
        dialog.onClose.append(back.unbind)
    except Exception:
        LOG.debug("a screen without onClose; PREVIOUS is EXIT on it only while it executes")
    return back
