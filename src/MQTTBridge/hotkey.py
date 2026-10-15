"""The key that opens the receiver menu, bound so that it takes nothing from the image.

**How enigma2 hands out a key.** Every key press goes to one dispatcher, `eActionMap`, which
holds bindings ordered by priority - the lowest number first - and, within one priority, by age.
A Python binding names a *context*; the keymaps map a key and its flags (make, break, repeat,
long) to an action name in that context; the binding's function is called with the context and
the action and returns whether it consumed the key. The first binding that answers yes ends the
press. The image's own screens bind their contexts while they execute, at priorities around 0
(the info bar's user-assignable button setup at -10); the info bar's "unhandled key" marker binds
the wildcard at the very end (`sys.maxsize`).

**What this module does** (ADR-0021):

- It maps the chosen key into a context of its own, `MQTTBridgeMenuActions`, with
  `eActionMap.bindKey` under a keymap domain of its own - at run time, so the setting decides the
  key and a change rebinds it, and `unbindKeyDomain` takes it all back. Nothing in the image's
  keymap is touched: its contexts never see the mapping.
- It binds that context at a priority just before the unhandled-key marker and after everything
  else (`PRIORITY`), so **every binding of the image is asked first**. Whatever the image or the
  user has put on the key - the image's own function, a plugin assigned in its button setup -
  runs, and the plugin never hears of the press.
- It answers yes only when it opened the menu: in live TV - the info bar is the executing screen
  - with the menu ready. Anywhere else it answers no and the key goes on as if the plugin were
  not there.
- Only the long press is mapped for HELP and RED, so a short press reaches nobody new. On every
  image read (OpenViX, OpenATV, OpenPLi) a long press suppresses the break that follows it for
  every binding, so the short-press function of the same key does not run after a long press.

F3 and F4 are keyboard keys, mapped on their break as the images' own button setups map them.

**What it cannot see.** A key the image gives a function through its user-assignable button setup
(`config.misc.ButtonSetup` on OpenViX and OpenATV, `config.misc.hotkey` on OpenPLi) is consumed
before this binding is asked, by design. `report()` reads those settings and says so in
`info.menu` - `hotkey` null with the reason - rather than claim a key that will never open the
menu. Any other image binding that consumes the key cannot be known in advance and is not
reported; the Extensions menu entry opens the menu whatever happens to the key.
"""

import sys

from .enigma2 import enigma_attribute, missing
from .keys import code_for
from .log import get_logger

LOG = get_logger("hotkey")

CONTEXT = "MQTTBridgeMenuActions"
DOMAIN = "MQTTBridge-menu-hotkey"
DEVICE = "generic"
# After every binding of the image, before the info bar's unhandled-key marker at `maxsize`.
# Correct on a 32-bit receiver too, where `maxsize` is 2**31 - 1.
PRIORITY = sys.maxsize - 1000

# The keymaps' flag bits: `1 << flag`, flag 0 make, 1 break, 2 repeat, 3 long.
FLAG_BREAK = 1 << 1
FLAG_LONG = 1 << 3

OFF = "off"
CHOICES = ("help_long", "red_long", "f3", "f4", OFF)

# choice -> (key name, flag bits, the action name in the context, the image's button-setup name)
BINDINGS = {
    "help_long": ("KEY_HELP", FLAG_LONG, "help_long", "displayHelp_long"),
    "red_long": ("KEY_RED", FLAG_LONG, "red_long", "red_long"),
    "f3": ("KEY_F3", FLAG_BREAK, "f3", "f3"),
    "f4": ("KEY_F4", FLAG_BREAK, "f4", "f4"),
}
# The Linux input codes, for an image whose key table does not name them.
FALLBACK_CODES = {"KEY_HELP": 138, "KEY_RED": 398, "KEY_F3": 61, "KEY_F4": 62}

SWITCHED_OFF = "the hotkey is switched off in the plugin's settings"
NOT_BINDABLE = "this image does not let a plugin bind a key"
ASSIGNED = "the receiver's own button setup gives this key a function"


def image_assignment(choice):
    """What the image's user-assignable button setup puts on the chosen key, or None.

    Read, never written: OpenViX's and OpenATV's `config.misc.ButtonSetup`, OpenPLi's
    `config.misc.hotkey`. An empty value is "nothing assigned"; an image without either has
    nothing to say.
    """
    binding = BINDINGS.get(choice)
    if binding is None:
        return None
    try:
        from Components.config import config
    except Exception:
        return None
    misc = getattr(config, "misc", None)
    for section in ("ButtonSetup", "hotkey"):
        element = getattr(getattr(misc, section, None), binding[3], None)
        value = getattr(element, "value", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _action_map():
    factory = enigma_attribute("eActionMap")
    if factory is None:
        return None
    try:
        return factory.getInstance()
    except Exception:
        LOG.exception("eActionMap.getInstance() raised")
        return None


class Hotkey:
    """One binding of one key to `on_press`, which says whether it opened the menu."""

    def __init__(self, on_press):
        self._on_press = on_press
        # One object for the life of the binding: the action map compares by identity when
        # unbinding (see `remote.py`).
        self._handler = self._action
        self.choice = OFF
        self.bound = None
        self.refusal = None
        self._bound_action = False

    def bind(self, choice):
        """Bind `choice` in place of whatever was bound. The bound choice, or None."""
        self.unbind()
        choice = choice if choice in CHOICES else OFF
        self.choice = choice
        self.bound = None
        self.refusal = None
        if choice == OFF:
            self.refusal = SWITCHED_OFF
            return None
        actions = _action_map()
        bind_key = getattr(actions, "bindKey", None) if actions is not None else None
        bind_action = getattr(actions, "bindAction", None) if actions is not None else None
        if bind_key is None or bind_action is None:
            missing("eActionMap.bindKey")
            self.refusal = NOT_BINDABLE
            return None
        name, flags, action, _setup = BINDINGS[choice]
        code = code_for(name) or FALLBACK_CODES[name]
        try:
            bind_key(DOMAIN, DEVICE, int(code), flags, CONTEXT, action)
            bind_action(CONTEXT, PRIORITY, self._handler)
        except Exception as error:
            missing("eActionMap.bindKey", error)
            self.refusal = NOT_BINDABLE
            self.unbind()
            return None
        self._bound_action = True
        self.bound = choice
        LOG.info("the menu hotkey is %s (%s)", choice, name)
        return choice

    def unbind(self):
        actions = _action_map()
        if actions is None:
            self._bound_action = False
            self.bound = None
            return
        if self._bound_action:
            try:
                actions.unbindAction(CONTEXT, self._handler)
            except Exception:
                LOG.debug("could not unbind the menu hotkey's context")
            self._bound_action = False
        unbind_domain = getattr(actions, "unbindKeyDomain", None)
        if unbind_domain is not None:
            try:
                unbind_domain(DOMAIN)
            except Exception:
                LOG.debug("could not take the menu hotkey's key mapping back")
        self.bound = None

    def report(self):
        """`(hotkey, reason)` for `info.menu`: the bound choice, or None and why not."""
        if self.bound is None:
            return None, self.refusal
        if image_assignment(self.bound) is not None:
            return None, ASSIGNED
        return self.bound, None

    def _action(self, context, action):
        """The dispatcher's call: 1 only when the menu opened. Never raises."""
        try:
            if context != CONTEXT or self.bound is None:
                return 0
            if action != BINDINGS[self.bound][2]:
                return 0
            return 1 if self._on_press() else 0
        except Exception:
            LOG.exception("the menu hotkey raised; the key goes on to the image")
            return 0
