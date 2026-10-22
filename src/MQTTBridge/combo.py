"""The press combo: a key pressed a number of times in quick succession opens the receiver menu.

A second way to open the menu, beside the hotkey (`hotkey.py`), for a household that drives the
receiver with the television's own remote over HDMI-CEC. Such a remote has no HELP, no colour
keys and no F keys, and CEC delivers no long press: a held Back arrives as a few short presses.
What it does deliver is its Back key - as `KEY_PREVIOUS` on some televisions, `KEY_EXIT` on
others - and its play keys. So the combo is one of those keys pressed `menu_combo_presses` times,
each press within `menu_combo_gap_ms` of the one before (ADR-0025).

**It takes its key from the image, where the hotkey takes nothing.** A key that only opened the
menu on the N-th press would still do the image's work on the first N-1: on OpenViX a `KEY_PREVIOUS`
in live TV zaps back in the history, so three presses would zap twice and then open the menu over
another channel. So while a key is set here, that key **only counts toward the menu in live TV
and the players** - the screens the hotkey opens the menu over (`menu.hotkey_screen`) - and every
event of it there is consumed: the make, the break, the repeats and the long press. Consuming
the makes alone would let a break or a long press reach an image binding that acts on it. In
every other screen - a menu, a list, the EPG, this plugin's own menu - the key goes on to the
image as before.

How that is done:

- The key is mapped at run time into a context of its own, `MQTTBridgeMenuComboActions`, under a
  keymap domain of its own, with one action per flag (`make`, `break`, `repeat`, `long`);
  `unbindKeyDomain` takes it all back. Nothing in the image's keymap is touched.
- The context is bound at `PRIORITY`, just after the key publisher (`remote.py`, which binds the
  wildcard at `-sys.maxsize - 1` and always answers no, so the `key` topic still reports every
  press) and before every binding of the image - its contexts sit around 0, its user-assignable
  button setup at -10. So the combo answers first, and what it consumes no image binding sees.
- **A press is a make.** Repeats of a held key are not presses. A CEC hold that arrives as several
  makes counts as several presses.
- **The window is measured make to make** on a monotonic clock. A make within the gap of the one
  before counts on; a later one starts a new count at one. On the N-th the menu opens - the
  hotkey's own open path - and the count starts again. The count also starts again when the key
  arrives over any other screen, when it arrives over a different live-TV or player screen than
  the press before, when the menu opens any other way, and on every rebind. No timer: a count
  that is given up simply did nothing.

`replaces` (`info.menu.combo.replaces`) names what the key no longer reaches in live TV and the
players. It is read when the key is bound, from the image's keymap - the mappings of the key in
every context the info bar and the players bind, recognised by name (`live_context`) - and, when
`info` is built, from the image's button setup (`config.misc.ButtonSetup`, `config.misc.hotkey`),
which a person may change at any time. Best effort, and never a reason not to bind: `None` when
the keymap cannot be read.
"""

import sys
import time
import xml.etree.ElementTree as ElementTree

from .enigma2 import enigma_attribute, missing
from .keys import code_for
from .log import get_logger

LOG = get_logger("combo")

CONTEXT = "MQTTBridgeMenuComboActions"
DOMAIN = "MQTTBridge-menu-combo"
DEVICE = "generic"
# After the key publisher's wildcard at `-sys.maxsize - 1`, before every binding of the image.
PRIORITY = -sys.maxsize

# enigma2's flag numbers, from the driver up; a keymap entry holds `1 << flag`.
FLAG_MAKE = 0
FLAG_BREAK = 1
FLAG_REPEAT = 2
FLAG_LONG = 3
# The action each flag is mapped to in the context: one per flag, so the handler knows which.
ACTIONS = {"make": FLAG_MAKE, "break": FLAG_BREAK, "repeat": FLAG_REPEAT, "long": FLAG_LONG}

OFF = "off"
# choice -> the key's name
KEYS = {
    "previous": "KEY_PREVIOUS",
    "exit": "KEY_EXIT",
    "playpause": "KEY_PLAYPAUSE",
    "stop": "KEY_STOP",
}
CHOICES = (OFF,) + tuple(KEYS)
# The Linux input codes, for an image whose key table does not name them.
FALLBACK_CODES = {"KEY_PREVIOUS": 412, "KEY_EXIT": 174, "KEY_PLAYPAUSE": 164, "KEY_STOP": 128}

# Where the image keeps its keymap, when `config.usage.keymap` does not say.
DEFAULT_KEYMAP = "/usr/share/enigma2/keymap.xml"
# The contexts of the image's user-assignable button setup: an action there is the name of a
# setting in `config.misc.ButtonSetup` (OpenViX, OpenATV) or `config.misc.hotkey` (OpenPLi).
BUTTON_SETUP_CONTEXTS = ("ButtonSetupActions", "HotkeyActions")
# The button setup's names for each key, for when the keymap cannot be read.
BUTTON_SETUP_NAMES = {
    "previous": ("previous",),
    "exit": (),
    "playpause": ("playpause",),
    "stop": ("stop", "shift_stop"),
}

SWITCHED_OFF = "the press combo is switched off in the plugin's settings"
NOT_BINDABLE = "this image does not let a plugin bind a key"

# The milliseconds between two presses are compared at this resolution, so that a gap measured
# as 1200.0000000002 ms is the 1200 ms it was.
_MS = 1000.0


def live_context(name):
    """Whether a keymap context is one the info bar or a player binds, by its name.

    The info bar's contexts carry its name (`InfobarChannelSelection`, `InfobarSeekActions`,
    `2ndInfobarActions`, ...); the movie player's and the media player's carry theirs.
    """
    return "infobar" in name.lower() or name.startswith(("MoviePlayer", "MediaPlayer"))


def keymap_path():
    """The keymap the image loaded: `config.usage.keymap` where it has one, else the default."""
    try:
        from Components.config import config

        configured = getattr(getattr(getattr(config, "usage", None), "keymap", None),
                             "value", None)
    except Exception:
        configured = None
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return DEFAULT_KEYMAP


def read_keymap(path, key_name):
    """What the keymap at `path` maps `key_name` to: `(functions, button_setup_names)`.

    `functions` are `"<context>.<action>"` in every context `live_context` accepts, in the
    file's order, each once; `button_setup_names` are the actions of the button setup's contexts
    on the key. None when the file cannot be read or parsed.
    """
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError, ValueError) as error:
        LOG.info("the keymap %s could not be read (%s); what the combo replaces is not known",
                 path, error)
        return None
    code = code_for(key_name) or FALLBACK_CODES.get(key_name)
    ids = {key_name}
    if code is not None:
        ids.update((str(code), hex(code)))
    functions = []
    setup_names = []
    for mapping in root.iter("map"):
        context = mapping.get("context") or ""
        for key in mapping.iter("key"):
            if key.get("id") not in ids or not key.get("mapto"):
                continue
            action = key.get("mapto")
            if context in BUTTON_SETUP_CONTEXTS:
                if action not in setup_names:
                    setup_names.append(action)
            elif live_context(context):
                function = context + "." + action
                if function not in functions:
                    functions.append(function)
    return functions, setup_names


def button_setup_assignments(names):
    """`ButtonSetup.<name>` for each of `names` the image's button setup gives a function."""
    try:
        from Components.config import config
    except Exception:
        return []
    misc = getattr(config, "misc", None)
    assigned = []
    for name in names:
        for section in ("ButtonSetup", "hotkey"):
            value = getattr(getattr(getattr(misc, section, None), name, None), "value", None)
            if isinstance(value, str) and value.strip():
                assigned.append("ButtonSetup." + name)
                break
    return assigned


def _action_map():
    factory = enigma_attribute("eActionMap")
    if factory is None:
        return None
    try:
        return factory.getInstance()
    except Exception:
        LOG.exception("eActionMap.getInstance() raised")
        return None


class Combo:
    """One binding of one key, counting its presses toward the menu.

    `screen()` answers the executing dialog when it is live TV or a player and the menu could
    open over it, else None; `open()` opens the menu and says whether it did.
    """

    def __init__(self, screen, open_menu):
        self._screen = screen
        self._open = open_menu
        # One object for the life of the binding: the action map compares by identity when
        # unbinding (see `remote.py`).
        self._handler = self._action
        self.clock = time.monotonic
        self.choice = OFF
        self.presses = None
        self.gap_ms = None
        self.bound = None
        self.refusal = SWITCHED_OFF
        self._bound_action = False
        self._keymap = None
        self._raised = False
        self.reset()

    # ------------------------------------------------------------ the binding --

    def bind(self, choice, presses, gap_ms):
        """Bind `choice` in place of whatever was bound. The bound choice, or None."""
        self.unbind()
        choice = choice if choice in KEYS else OFF
        self.choice = choice
        self.presses = presses
        self.gap_ms = gap_ms
        self.refusal = None
        self._keymap = None
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
        name = KEYS[choice]
        code = code_for(name) or FALLBACK_CODES[name]
        try:
            for action, flag in ACTIONS.items():
                bind_key(DOMAIN, DEVICE, int(code), 1 << flag, CONTEXT, action)
            bind_action(CONTEXT, PRIORITY, self._handler)
        except Exception as error:
            missing("eActionMap.bindKey", error)
            self.refusal = NOT_BINDABLE
            self.unbind()
            return None
        self._bound_action = True
        self.bound = choice
        self._keymap = read_keymap(keymap_path(), name)
        LOG.info("the menu's press combo is %s (%s) %d times within %d ms", choice, name,
                 presses, gap_ms)
        return choice

    def unbind(self):
        self.reset()
        actions = _action_map()
        if actions is None:
            self._bound_action = False
            self.bound = None
            return
        if self._bound_action:
            try:
                actions.unbindAction(CONTEXT, self._handler)
            except Exception:
                LOG.debug("could not unbind the press combo's context")
            self._bound_action = False
        unbind_domain = getattr(actions, "unbindKeyDomain", None)
        if unbind_domain is not None:
            try:
                unbind_domain(DOMAIN)
            except Exception:
                LOG.debug("could not take the press combo's key mapping back")
        self.bound = None

    def settings(self):
        """What the combo was last asked to bind: `(choice, presses, gap_ms)`."""
        return (self.choice, self.presses, self.gap_ms)

    # ------------------------------------------------------------- `info` --

    def replaces(self):
        """The image functions the key no longer reaches in live TV and the players, or None."""
        if self._keymap is None:
            return None
        functions, setup_names = self._keymap
        return list(functions) + button_setup_assignments(
            setup_names or BUTTON_SETUP_NAMES.get(self.bound, ())
        )

    def report(self):
        """`(combo, combo_reason)` for `info.menu`."""
        if self.bound is None:
            return None, self.refusal
        return {
            "key": self.bound,
            "presses": self.presses,
            "gap_ms": self.gap_ms,
            "replaces": self.replaces(),
        }, None

    # ----------------------------------------------------------- the counting --

    def reset(self):
        """Start counting again from no press."""
        self.count = 0
        self._last = None
        self._dialog = None

    def _counted(self, dialog):
        """One make over `dialog`: True when it is the N-th within the gaps."""
        now = self.clock()
        if (
            self._last is not None
            and self._dialog == id(dialog)
            and round((now - self._last) * _MS) <= self.gap_ms
        ):
            self.count += 1
        else:
            self.count = 1
        self._last = now
        self._dialog = id(dialog)
        if self.count >= self.presses:
            self.reset()
            return True
        return False

    def _action(self, context, action):
        """The dispatcher's call: 1 for every event of the key over live TV or a player.

        Never raises: on an exception the key goes on to the image (0), logged once.
        """
        try:
            if context != CONTEXT or self.bound is None or action not in ACTIONS:
                return 0
            dialog = self._screen()
            if dialog is None:
                # Another screen has the key, and a count that left live TV ends here.
                self.reset()
                return 0
            if ACTIONS[action] == FLAG_MAKE and self._counted(dialog):
                try:
                    self._open()
                except Exception:
                    LOG.exception("the press combo could not open the menu")
            return 1
        except Exception:
            if not self._raised:
                self._raised = True
                LOG.exception("the press combo raised; the key goes on to the image")
            return 0

