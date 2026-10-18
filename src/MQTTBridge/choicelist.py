"""A list of choices on the television, opened by Home Assistant - `cmd/list`.

Home Assistant puts a question on the screen as a list of labelled rows - "Doorbell: open the
gate / ignore" - and the viewer picks one with the remote. The receiver says **which row id** was
picked, once, on `<base>/<node>/list_answer`, and nothing else: it never learns what a row does
and never calls a service. A yes/no question is a list of two rows (ADR-0023,
docs/TOPICS.md).

Four parts live here:

- **The payload** (`parse_list`): every member of the contract checked strictly, before anything
  is shown. A refusal names the member and the row's index and never the sender's text. Members
  this plugin does not know are ignored, so a newer sender can add some; the log says so once a
  run, with a count. Text loses every backslash, for the toast's reason
  (`toast.strip_backslashes`), and its invisible format characters, as the menu's does
  (`menu.clean_text`); a title and a label are one line each.
- **One list** (`ListController`): what the screen draws and what each key does. OK picks the
  selected row; a row's colour or number key picks that row at once; EXIT dismisses; every other
  key does nothing. Every way a list ends goes through `ListPublisher.end`, which answers once
  and only once.
- **The screens.** The plugin's own (`MQTTBridgeChoices`): the receiver menu's look and machinery
  (`styledlist.py`) - computed geometry, the menu's icons, its see-through background
  (`menu_transparency`) - with a header line that says the question comes from Home Assistant
  through this plugin, the title, the text, the rows, the remaining time and the keys' legend.
  Built once on trial off the dialog stack, as the menu is, and only then opened. Where it cannot
  be built, the image's own `ChoiceBox` with the same rows, ids and keys.
- **The publisher** (`ListPublisher`): the capability `list`, `info.list`, one open list at a
  time, the countdown, standby, the plugin's stop, and the answer.

**How a list ends, and its one answer.** `picked` with the row, `timeout` with the default row or
none, `dismissed` (EXIT), `replaced` (a newer list arrived), `closed` (standby, the plugin
stopping, or the screen closed by something else). The answer goes out as an event: QoS 1, never
retained, and never queued across a disconnect - one the broker had not acknowledged when the
connection went is taken out of the client's queue, as `menu_action` is. A late "open the gate"
is worse than none.

**Over whatever is on screen.** The list is a modal screen opened on top of the dialog that is
executing - the info bar, a player, the receiver menu, the image's keyboard, any other screen -
which keeps its place underneath and gets the keys back when the list closes. While the session
is between two screens (a close is popped on the next turn of the main loop) the list waits for
it, at most two seconds. A list that can be put on the screen by neither screen is a refusal on
`last_error`, with the command's id, and no answer. The power key does on the list what it does
on the screen under it: the list copies that screen's `ALLOW_SUSPEND`, and hands a player's pause
on to it.

Everything here runs on enigma2's main loop and nothing raises into it.
"""

import json
import math
import os
import re
import time
import unicodedata

from . import styledlist
from .enigma2 import Ticker, enigma_attribute, missing
from .i18n import _
from .log import get_logger
from .menu import DROPPED_CATEGORIES, ICON_NAMES, NEUTRAL_ICON, clean_text
from .publisher import Publisher, Refusal
from .toast import strip_backslashes

LOG = get_logger("choicelist")

CAPABILITY = "list"
COMMAND = "list"
# The answer, in the node's tree. An event: QoS 1, never retained, never queued.
ANSWER_SUFFIX = "list_answer"
ANSWER_QOS = 1

STYLED = "styled"
CHOICEBOX = "choicebox"

# What a skin calls the screen if it wants to restyle it.
SKIN_NAME = "MQTTBridgeChoices"

# The contract's bounds.
TITLE_LIMIT = 60
TEXT_LIMIT = 200
LABEL_LIMIT = 60
MAX_ROWS = 24
ROW_ID = re.compile(r"[A-Za-z0-9._:-]{1,32}")
COLOUR_KEYS = ("red", "green", "yellow", "blue")
NUMBER_KEYS = tuple(str(number) for number in range(1, 10))
ROW_KEYS = COLOUR_KEYS + NUMBER_KEYS
DEFAULT_TIMEOUT = 60
MIN_TIMEOUT = 5
MAX_TIMEOUT = 600
PAYLOAD_MEMBERS = frozenset(("title", "text", "rows", "timeout", "default"))
ROW_MEMBERS = frozenset(("id", "label", "key", "icon"))

# How a list ends.
PICKED = "picked"
TIMEOUT = "timeout"
DISMISSED = "dismissed"
REPLACED = "replaced"
CLOSED = "closed"
ENDINGS = (PICKED, TIMEOUT, DISMISSED, REPLACED, CLOSED)
# Not an ending: a list that could not be put on the screen, which is refused, not answered.
NOT_SHOWN_MARK = "not_shown"

# The refusals, and their reason codes on `last_error`.
INVALID = "invalid"
STANDBY = "the receiver is in standby"
STANDBY_REASON = "standby"
NOT_AVAILABLE = "lists cannot be shown on this receiver"
NOT_AVAILABLE_REASON = "no_capability"
NOT_SHOWN = "the receiver could not show the list"
NOT_SHOWN_REASON = "not_shown"
# The reason the dispatcher gives the doors' sentence for this command (`commands.py`).
BUSY_REASON = "busy"

# While the session is between two screens, a list waits for it: this often, this many times.
OPEN_RETRY_MILLISECONDS = 50
OPEN_RETRY_LIMIT = 40

# The shape at a 720-line desktop; every number is multiplied by `height / 720`, as the menu's
# (`styledlist.BASE`) - the same width, padding, rows and icons, with the lines a question needs.
BASE_LINES = 720
BASE = {
    "width": 640,
    "pad": 16,
    "header": 24,
    "title": 64,
    "title_line": 32,
    "text": 120,
    "row": 46,
    "icon": 30,
    "key": 30,
    "gap": 6,
    "countdown": 28,
    "legend": 24,
    "header_font": 16,
    "title_font": 26,
    "text_font": 20,
    "row_font": 22,
    "state_font": 20,
    "countdown_font": 18,
    "legend_font": 16,
}
VISIBLE_ROWS = 8
TEXT_COLOUR = "#00e8eef2"
# The widgets made as tall as their text after the layout.
FITTED = ("title", "text", "list", "countdown", "legend")

# A row's key, on its right: one 64-px PNG for each colour's button and one box for a digit,
# drawn by `tools/render-keys.py`. The colours are those the PNGs are drawn in; a filled
# rectangle of the same colour is the second way. The button fills this much of its square.
KEY_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys")
KEY_RGB = {"red": 0xE5534B, "green": 0x4CAF50, "yellow": 0xE8C547, "blue": 0x4C8FE0}
BUTTON_SHARE = 0.625

_screen_class = None


class InvalidList(ValueError):
    """The payload breaks a rule of the contract; the sentence names the member, never its text."""


# ------------------------------------------------------------------- the text --


def clean_line(value, limit):
    """A title or a label, by the contract's text rule, in its order.

    1. every backslash removed; 3. every format character (Cf) and lone surrogate (Cs)
    dropped; 4. every control character and line or paragraph separator (Cc, Zl, Zp) a space;
    5. cut at `limit`; 6. leading and trailing whitespace stripped - empty is blank. Step 2 is
    the text's alone (`clean_lines`). The menu's own text rule is steps 1, 3, 4 and 5
    (`menu.clean_text`), so a label is drawn the same in both.
    """
    return clean_text(value, limit).strip()


def clean_lines(value, limit):
    """The text under the title: as `clean_line`, and 2. `
` and `
` become `
`, which
    stays a line break in step 4."""
    text = strip_backslashes(str(value)).replace("\r\n", "\n").replace("\r", "\n")
    pieces = []
    for character in text:
        category = unicodedata.category(character)
        if category in DROPPED_CATEGORIES:
            continue
        if character == "\n":
            pieces.append(character)
        elif category in ("Cc", "Zl", "Zp"):
            pieces.append(" ")
        else:
            pieces.append(character)
    return "".join(pieces)[:limit].strip()


# ---------------------------------------------------------------- the payload --


def _whole_number(value):
    """A JSON integer: not a bool, not a fraction, not a string."""
    return isinstance(value, int) and not isinstance(value, bool)


def _required_line(value, name, limit):
    if value is None:
        raise InvalidList(name + " is missing")
    if not isinstance(value, str):
        raise InvalidList(name + " is not text")
    line = clean_line(value, limit)
    if not line:
        raise InvalidList(name + " is empty")
    return line


def _optional_text(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidList("text is not text or null")
    return clean_lines(value, TEXT_LIMIT) or None


def _icon(value, where):
    """None for no icon; a name of the icon list; the neutral icon for any other name."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidList(where + ".icon is not text or null")
    return value if value in ICON_NAMES else NEUTRAL_ICON


def parse_list(text):
    """`(spec, unknown)`: the list a payload asks for, and how many members were not known.

    Raises InvalidList, with a sentence that names the member and the row's index - never the
    sender's text - for anything that breaks a rule. A row's index counts from 0, as JSON does.
    """
    stripped = str(text or "").strip()
    try:
        raw = json.loads(stripped) if stripped else None
    except (ValueError, RecursionError):
        raw = None
    if not isinstance(raw, dict):
        raise InvalidList("cmd/list takes a JSON object")
    unknown = len(set(raw) - PAYLOAD_MEMBERS)
    title = _required_line(raw.get("title"), "title", TITLE_LIMIT)
    body = _optional_text(raw.get("text"))
    members = raw.get("rows")
    if not isinstance(members, list):
        raise InvalidList("rows is not a list")
    if not members:
        raise InvalidList(f"rows is empty; a list has 1 to {MAX_ROWS} rows")
    if len(members) > MAX_ROWS:
        raise InvalidList(f"rows has more than {MAX_ROWS} rows")
    rows = []
    ids = set()
    keys = set()
    for index, member in enumerate(members):
        where = f"rows[{index}]"
        if not isinstance(member, dict):
            raise InvalidList(where + " is not an object")
        unknown += len(set(member) - ROW_MEMBERS)
        ident = member.get("id")
        if not isinstance(ident, str) or not ROW_ID.fullmatch(ident):
            raise InvalidList(where + ".id must be 1 to 32 of A-Z, a-z, 0-9, '.', '_', ':' "
                              "and '-'")
        if ident in ids:
            raise InvalidList(where + ".id is the id of an earlier row")
        ids.add(ident)
        label = _required_line(member.get("label"), where + ".label", LABEL_LIMIT)
        key = member.get("key")
        if key is not None:
            if not isinstance(key, str) or key not in ROW_KEYS:
                raise InvalidList(where + '.key is not red, green, yellow, blue or "1" to "9"')
            if key in keys:
                raise InvalidList(where + ".key is the key of an earlier row")
            keys.add(key)
        rows.append({"id": ident, "label": label, "key": key,
                     "icon": _icon(member.get("icon"), where)})
    timeout = DEFAULT_TIMEOUT
    if "timeout" in raw:
        timeout = raw["timeout"]
        if not _whole_number(timeout) or not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
            raise InvalidList("timeout must be a whole number of seconds from "
                              f"{MIN_TIMEOUT} to {MAX_TIMEOUT}")
    default = raw.get("default")
    if default is not None and (not isinstance(default, str) or default not in ids):
        raise InvalidList("default is not the id of a row")
    spec = {"title": title, "text": body, "rows": rows, "timeout": timeout, "default": default}
    return spec, unknown


# ------------------------------------------------------------------ the words --


def countdown_text(seconds, default_label=None):
    """The remaining time; with a default row, what the timeout will answer, by its label."""
    seconds = max(0, int(seconds))
    if default_label:
        return _("%(label)s in %(seconds)d s") % {"label": default_label, "seconds": seconds}
    return _("Closes in %d s") % seconds


def header_text():
    return _("From Home Assistant, via MQTT Bridge")


def legend_text(with_keys):
    if with_keys:
        return _("OK or the row's key choose   EXIT dismiss")
    return _("OK choose   EXIT dismiss")


# ------------------------------------------------------------------- one list --


class ListController:
    """One list, from the command that asked for it to its one answer.

    The screen asks `view()` for what to draw and hands every key to one of the `key_*`
    methods; a key that ends the list asks the publisher to end it. `ended` is the ending once
    there is one, and from then on no key does anything.
    """

    def __init__(self, publisher, spec, command_id=None):
        self.publisher = publisher
        self.spec = spec
        self.command_id = command_id
        self.ended = None
        self.deadline = None
        self.screen = None
        # The screen the list was opened over: what the power key does there, and its pause.
        self.under = None
        self.suspend = None

    @property
    def ids(self):
        return [row["id"] for row in self.spec["rows"]]

    def label(self):
        """How the log names this list: by the command's id, never by its text."""
        return "(id " + self.command_id + ")" if self.command_id else "(no id)"

    def remaining(self):
        """Whole seconds left, rounded up; the whole timeout before the list is shown."""
        if self.deadline is None:
            return self.spec["timeout"]
        left = self.deadline - self.publisher.clock()
        return max(0, int(math.ceil(left - 1e-9)))

    def default_label(self):
        for row in self.spec["rows"]:
            if row["id"] == self.spec["default"]:
                return row["label"]
        return None

    def countdown(self):
        return countdown_text(self.remaining(), self.default_label())

    def initial(self):
        return self.spec["default"] or self.spec["rows"][0]["id"]

    def view(self):
        rows = self.spec["rows"]
        with_icons = any(row["icon"] for row in rows)
        return {
            "header": header_text(),
            "title": self.spec["title"],
            "text": self.spec["text"] or "",
            "rows": [
                {"id": row["id"], "icon": row["icon"], "icon_space": with_icons,
                 "label": row["label"], "key": row["key"]}
                for row in rows
            ],
            "countdown": self.countdown(),
            "legend": legend_text(any(row["key"] for row in rows)),
            "initial": self.initial(),
        }

    def key_of(self, ident):
        for row in self.spec["rows"]:
            if row["id"] == ident:
                return row["key"]
        return None

    # ------------------------------------------------------------------ keys --

    def key_ok(self, selected):
        """OK: the selected row is the answer. A selection that is no row does nothing."""
        if self.ended is None and selected in self.ids:
            self.publisher.end(self, PICKED, selected)

    def key_cancel(self, _selected=None):
        if self.ended is None:
            self.publisher.end(self, DISMISSED)

    def key_named(self, key):
        """A colour key, or a digit: the row that has it is the answer; no row, nothing."""
        if self.ended is not None:
            return
        for row in self.spec["rows"]:
            if row["key"] == key:
                self.publisher.end(self, PICKED, row["id"])
                return

    def key_number(self, number):
        self.key_named(str(number))


class _Trial:
    """The trial build's controller: the same view, and nothing it does reaches a list."""

    trial = True
    ended = None
    suspend = None
    under = None

    def __init__(self, controller):
        self._controller = controller

    def view(self):
        return self._controller.view()

    def __getattr__(self, name):
        if name.startswith("key_"):
            return lambda *arguments: None
        raise AttributeError(name)


# -------------------------------------------------------------- the geometry --


def geometry(desktop_width, desktop_height, rows, has_text):
    """Every number the skin and the rows need, as plain desktop pixels.

    The title has room for two lines - 60 characters do not fit one - and the text for five,
    200 characters with room to spare. Both are laid out at that size and then made as tall as
    their text once the skin is applied (`MQTTBridgeChoices._fit`), as the toast sizes itself.
    The text's label is always there, `0` high without a text, so every list has the same
    widgets and a skin that restyles the screen restyles all of them.
    """
    factor = float(desktop_height) / BASE_LINES
    shape = {name: int(value * factor) for name, value in BASE.items()}
    shape["width"] = max(1, min(shape["width"], int(desktop_width) - 2 * shape["pad"]))
    shape["inner"] = max(1, shape["width"] - 2 * shape["pad"])
    visible = max(1, min(int(rows), VISIBLE_ROWS))
    shape["header_y"] = shape["pad"]
    shape["title_y"] = shape["header_y"] + shape["header"]
    shape["text_y"] = shape["title_y"] + shape["title"]
    shape["text_height"] = shape["text"] if has_text else 0
    # Everything scales with the desktop's height, so eight rows and the tallest title and
    # text fit any desktop: 672 of 720 lines.
    shape["visible"] = visible
    shape["list_y"] = shape["text_y"] + shape["text_height"] + shape["gap"]
    shape["list_height"] = visible * shape["row"]
    shape["countdown_y"] = shape["list_y"] + shape["list_height"] + shape["gap"]
    shape["legend_y"] = shape["countdown_y"] + shape["countdown"]
    shape["height"] = shape["legend_y"] + shape["legend"] + shape["pad"]
    shape["x"] = max(0, (int(desktop_width) - shape["width"]) // 2)
    shape["y"] = max(0, (int(desktop_height) - shape["height"]) // 2)
    shape["desktop_height"] = int(desktop_height)
    shape["factor"] = factor
    return shape


def desktop_shape(spec):
    get_desktop = enigma_attribute("getDesktop")
    if get_desktop is None:
        return None
    size = get_desktop(0).size()
    return geometry(size.width(), size.height(), len(spec["rows"]), bool(spec["text"]))


def build_skin(shape):
    """The embedded skin, every number a plain integer."""
    percent = shape.get("transparency", 0)
    values = dict(
        shape,
        name=SKIN_NAME,
        background=styledlist.background_colour(styledlist.BACKGROUND_RGB, percent),
        panel=styledlist.background_colour(styledlist.PANEL_RGB, percent),
        selected=styledlist.SELECTED,
        title_colour=styledlist.TITLE_COLOUR,
        marker_colour=styledlist.MARKER_COLOUR,
        text_colour=TEXT_COLOUR,
        notice_colour=styledlist.NOTICE_COLOUR,
        legend_colour=styledlist.LEGEND_COLOUR,
    )
    return (
        '<screen name="{name}" position="{x},{y}" size="{width},{height}" '
        'flags="wfNoBorder" backgroundColor="{background}">'
        '<widget name="header" position="{pad},{header_y}" size="{inner},{header}" '
        'font="Regular;{header_font}" valign="center" foregroundColor="{marker_colour}" '
        'backgroundColor="{background}" transparent="1" />'
        '<widget name="title" position="{pad},{title_y}" size="{inner},{title}" '
        'font="Regular;{title_font}" valign="top" foregroundColor="{title_colour}" '
        'backgroundColor="{background}" transparent="1" />'
        '<widget name="text" position="{pad},{text_y}" size="{inner},{text_height}" '
        'font="Regular;{text_font}" valign="top" foregroundColor="{text_colour}" '
        'backgroundColor="{background}" transparent="1" />'
        '<widget name="list" position="{pad},{list_y}" size="{inner},{list_height}" '
        'itemHeight="{row}" scrollbarMode="showOnDemand" backgroundColor="{panel}" '
        'backgroundColorSelected="{selected}" transparent="0" />'
        '<widget name="countdown" position="{pad},{countdown_y}" size="{inner},{countdown}" '
        'font="Regular;{countdown_font}" valign="center" foregroundColor="{notice_colour}" '
        'backgroundColor="{background}" transparent="1" />'
        '<widget name="legend" position="{pad},{legend_y}" size="{inner},{legend}" '
        'font="Regular;{legend_font}" valign="center" foregroundColor="{legend_colour}" '
        'backgroundColor="{background}" transparent="1" />'
        "</screen>"
    ).format(**values)


# --------------------------------------------------------------------- the rows --


def key_path(name):
    """The PNG of a key's button: `red`, `green`, `yellow`, `blue`, or `box` for a digit."""
    return os.path.join(KEY_DIRECTORY, name + ".png")


def build_row(row, shape, load, load_key, names):
    """One list entry: `[id, icon?, label, button?, digit?]`, in the image's multi-content form.

    The icon as the menu draws it (`styledlist._pixmap_entry`, scaled where the image can);
    a row without an icon keeps the icon's room when another row has one, so the labels line
    up. The key on the right: a filled rounded button in the key's colour, or the digit in a
    small box - one 64-px PNG each, scaled like the icons. Where the image cannot place a
    pixmap, a colour key is a filled rectangle of its colour (a text entry's background) and a
    digit is the digit alone; no colour is ever written as a word.
    """
    multicontent = names["multicontent"]
    text_entry = multicontent.MultiContentEntryText
    height = shape["row"]
    pad = shape["pad"]
    x = pad // 2
    entry = [row["id"]]
    if row.get("icon"):
        size = shape["icon"]
        made = styledlist._pixmap_entry(multicontent, names["scale_flags"],
                                        (x, (height - size) // 2), (size, size),
                                        load(row["icon"]))
        if made is not None:
            entry.append(made)
        x += size + pad // 2
    elif row.get("icon_space"):
        x += shape["icon"] + pad // 2
    key = row.get("key")
    side = shape["key"]
    right = shape["inner"] - pad // 2
    label_right = right - side - pad // 2 if key else right
    entry.append(text_entry(
        pos=(x, 0), size=(max(1, label_right - x), height), font=0,
        flags=names["left"] | names["vcenter"] | names["ellipsis"],
        text=row.get("label") or "",
        color=styledlist.TEXT, color_sel=styledlist.TEXT_SELECTED,
    ))
    if not key:
        return entry
    position = (right - side, (height - side) // 2)
    made = styledlist._pixmap_entry(multicontent, names["scale_flags"], position, (side, side),
                                    load_key("box" if key in NUMBER_KEYS else key))
    if made is not None:
        entry.append(made)
    elif key in COLOUR_KEYS:
        # The second way: the button's own rectangle, filled with its colour, no corners.
        band = int(side * BUTTON_SHARE)
        try:
            entry.append(text_entry(
                pos=(position[0], position[1] + (side - band) // 2), size=(side, band), font=1,
                flags=0, text="", color=KEY_RGB[key], color_sel=KEY_RGB[key],
                backcolor=KEY_RGB[key], backcolor_sel=KEY_RGB[key],
            ))
        except Exception:
            LOG.debug("the image draws no filled key either; the row has none")
    if key in NUMBER_KEYS:
        entry.append(text_entry(
            pos=position, size=(side, side), font=1,
            flags=names["center"] | names["vcenter"], text=key,
            color=styledlist.TEXT, color_sel=styledlist.TEXT_SELECTED,
        ))
    return entry


def _names():
    names = styledlist._names()
    names["center"] = styledlist._optional_flag("RT_HALIGN_CENTER")
    return names


# ------------------------------------------------------------------ the screen --


def _suspend_of(dialog):
    """The `ALLOW_SUSPEND` the list takes over from the screen under it, or None.

    The image's power key opens standby only over a screen that allows it, and stops or
    pauses what that screen plays as the screen says. A list over the info bar lets the power
    key do what it does there; over a screen that does not allow it, nothing, as before. A
    player that pauses is handed its pause through the list, and one without the methods for
    it is not offered standby from under a list.
    """
    value = getattr(dialog, "ALLOW_SUSPEND", None)
    if not value:
        return None
    try:
        from Screens.Screen import Screen

        pauses = getattr(Screen, "SUSPEND_PAUSES", 2)
    except Exception:
        pauses = 2
    if value == pauses and not (callable(getattr(dialog, "pauseService", None))
                                and callable(getattr(dialog, "unPauseService", None))):
        return None
    return value


def screen_class():
    """The styled list's class, built on first use so importing this needs nothing."""
    global _screen_class
    if _screen_class is not None:
        return _screen_class

    from Components.ActionMap import ActionMap, NumberActionMap
    from Components.Label import Label
    from Components.MenuList import MenuList
    from enigma import eListboxPythonMultiContent, gFont
    from Screens.Screen import Screen
    from Tools.LoadPixmap import LoadPixmap

    class MQTTBridgeChoices(Screen):
        """A header line, the title, the text, the rows, the countdown and the legend."""

        def __init__(self, session, shape, controller):
            self.skin = build_skin(shape)
            Screen.__init__(self, session)
            self.shape = shape
            self.controller = controller
            self._trial = bool(getattr(controller, "trial", False))
            self._names = _names()
            self._pixmaps = {}
            self._ids = []
            if controller.suspend:
                # Over the info bar the power key opens standby here as it would there.
                self.ALLOW_SUSPEND = controller.suspend
            self["header"] = Label("")
            self["title"] = Label("")
            self["text"] = Label("")
            self["list"] = MenuList([], True, content=eListboxPythonMultiContent)
            content = self["list"].l
            content.setFont(0, gFont("Regular", shape["row_font"]))
            content.setFont(1, gFont("Regular", shape["state_font"]))
            content.setItemHeight(shape["row"])
            self["countdown"] = Label("")
            self["legend"] = Label("")
            self["listActions"] = ActionMap(
                ["OkCancelActions"], {"ok": self.key_ok, "cancel": self.key_cancel}, -1
            )
            self["listColours"] = ActionMap(
                ["ColorActions"], {name: self._colour(name) for name in COLOUR_KEYS}, -1
            )
            self["listNumbers"] = NumberActionMap(
                ["NumberActions"], {key: self.key_number for key in NUMBER_KEYS}, -1
            )
            if not self._trial:
                controller.screen = self
            # Built here, so that the trial build draws every row once.
            self.render(controller.view())
            # A move before the skin is applied is dropped by every image's `MenuList`, and
            # the text can be measured only in its widget.
            self.onLayoutFinish.append(self._laid_out)
            self.onClose.append(self._closed)

        def _laid_out(self):
            try:
                self.render(self.controller.view())
                self._fit()
            except Exception:
                LOG.exception("the list could not be drawn after its layout")

        # ---------------------------------------------------------- drawing --

        def _load(self, path):
            if path not in self._pixmaps:
                try:
                    self._pixmaps[path] = LoadPixmap(path)
                except Exception:
                    LOG.debug("a picture of the list could not be loaded")
                    self._pixmaps[path] = None
            return self._pixmaps[path]

        def _pixmap(self, name):
            return self._load(styledlist.icon_path(name))

        def _key_pixmap(self, name):
            return self._load(key_path(name))

        def render(self, view):
            self["header"].setText(view.get("header") or "")
            self["title"].setText(view.get("title") or "")
            self["text"].setText(view.get("text") or "")
            self["countdown"].setText(view.get("countdown") or "")
            self["legend"].setText(view.get("legend") or "")
            rows = view.get("rows") or []
            entries = [build_row(row, self.shape, self._pixmap, self._key_pixmap, self._names)
                       for row in rows]
            self._ids = [row["id"] for row in rows]
            self["list"].setList(entries)
            wanted = view.get("initial")
            if wanted in self._ids:
                self["list"].moveToIndex(self._ids.index(wanted))

        def _fit(self):
            """The title and the text as tall as they are, and everything under them moved up.

            Measured in their own widgets, at the width the skin gave them, as the toast
            measures its message (`calculateSize`); never taller than the room the skin gave.
            Only on the plugin's own layout: a skin that restyles the screen keeps its own.
            """
            shape = self.shape
            window = self.instance
            size_class = enigma_attribute("eSize")
            point_class = enigma_attribute("ePoint")
            if window is None or size_class is None or point_class is None:
                return
            if window.size() != size_class(shape["width"], shape["height"]):
                return
            if any(self[name].instance is None for name in FITTED):
                return

            def measured(name, room, least):
                widget = self[name]
                widget.instance.resize(size_class(shape["inner"], max(1, room)))
                if not widget.getText():
                    return 0
                return max(least, min(room, int(widget.getSize()[1])))

            title = measured("title", shape["title"], shape["title_line"])
            text = measured("text", shape["text"], 0) if shape["text_height"] else 0
            y = shape["title_y"]
            for name, height in (("title", title), ("text", text)):
                self[name].instance.resize(size_class(shape["inner"], max(0, height)))
                self[name].instance.move(point_class(shape["pad"], y))
                y += height
            y += shape["gap"]
            for name, height in (("list", shape["list_height"] + shape["gap"]),
                                 ("countdown", shape["countdown"]),
                                 ("legend", shape["legend"] + shape["pad"])):
                self[name].instance.move(point_class(shape["pad"], y))
                y += height
            window.resize(size_class(shape["width"], y))
            window.move(point_class(shape["x"], max(0, (shape["desktop_height"] - y) // 2)))

        def countdown(self, text):
            self["countdown"].setText(text)

        def selected(self):
            try:
                entry = self["list"].getCurrent()
            except Exception:
                return None
            return entry[0] if entry else None

        def show(self):
            # An ended list that comes back on top - from under the standby screen - closes
            # on that same `execBegin`, and is never drawn again on its way out.
            if getattr(self.controller, "ended", None) is not None:
                return
            Screen.show(self)

        # ------------------------------------------------------------- keys --

        def _ended(self):
            """True for a list that has its answer: the key closes the screen, if it is open."""
            if self.controller.ended is None:
                return False
            self.close_list()
            return True

        def key_ok(self):
            try:
                if not self._ended():
                    self.controller.key_ok(self.selected())
            except Exception:
                LOG.exception("OK on the list raised")

        def key_cancel(self):
            try:
                if not self._ended():
                    self.controller.key_cancel(self.selected())
            except Exception:
                LOG.exception("EXIT on the list raised; closing it")
                self.close_list()

        def _colour(self, name):
            def pressed():
                try:
                    if not self._ended():
                        self.controller.key_named(name)
                except Exception:
                    LOG.exception("a colour key on the list raised")

            return pressed

        def key_number(self, number):
            try:
                if not self._ended():
                    self.controller.key_number(int(number))
            except Exception:
                LOG.exception("a number key on the list raised")

        # ------------------------------------------------- the power key --

        def pauseService(self):
            self._under("pauseService")

        def unPauseService(self):
            self._under("unPauseService")

        def _under(self, name):
            try:
                getattr(self.controller.under, name)()
            except Exception:
                LOG.exception("the screen under the list could not %s", name)

        # ------------------------------------------------------------ close --

        def close_list(self):
            """Close the screen now, or - under another screen - when it is on top again."""
            try:
                if getattr(self, "execing", False) and \
                        getattr(self.session, "current_dialog", self) is not self:
                    self.close_on_next_exec = ()
                else:
                    self.close()
            except Exception:
                LOG.exception("the list screen could not be closed")

        def _closed(self):
            if not self._trial:
                self.controller.publisher.screen_gone(self.controller)

    _screen_class = MQTTBridgeChoices
    return _screen_class


def _can_open(session):
    """Whether `session.open` would take a screen now: not between two screens."""
    return not (getattr(session, "dialog_stack", None)
                and getattr(session, "in_exec", None) is False)


def open_styled(session, controller, transparency, callback):
    """Open the styled screen for `controller`. None when it is open, otherwise why not.

    Built once on trial with `instantiateDialog` - off the dialog stack - and deleted, then
    opened for real, as the menu is (`styledlist.open_styled`).
    """
    try:
        shape = desktop_shape(controller.spec)
        if shape is None:
            return "the image will not say how big its desktop is"
        shape["transparency"] = transparency
        cls = screen_class()
        trial = session.instantiateDialog(cls, shape, _Trial(controller))
        if trial is None:
            return "the image returned no screen"
        session.deleteDialog(trial)
    except Exception as error:
        LOG.exception("the styled list screen could not be built")
        return f"{type(error).__name__}: {error}"
    executing = getattr(session, "in_exec", None)
    try:
        session.openWithCallback(callback, cls, shape, controller)
    except Exception as error:
        LOG.exception("the styled list screen could not be opened")
        controller.screen = None
        styledlist._restore_executing(session, executing)
        return f"{type(error).__name__}: {error}"
    return None


class ChoiceBoxList:
    """The image's own `ChoiceBox` in the role of the styled screen.

    The same rows, ids and keys: the image's list picks a row on its colour or number key, as
    the styled one does. No icon. The question is the header line, the title and the text,
    one under the other. What the image makes of it, read in the three images' sources:

    - **OpenViX** puts a first line shorter than 55 characters into the window title and the
      rest into its text; **OpenATV** puts the first line into the window title and the rest
      into its text whatever its length. The window title is then the header line, and the
      countdown is set as "<header> - <countdown>", so it never takes the header's place.
    - **OpenPLi** puts the whole question into its text and the window title is its own
      ("Select"): the header is in the text already, and the window title is the countdown.

    The image is not asked which it is: after the list is open, its text is read, and a text
    that begins with the header line is the OpenPLi case. An image whose list takes no title
    shows no countdown.
    """

    def __init__(self, session, controller, callback):
        self.session = session
        self.controller = controller
        self.callback = callback
        self.dialog = None
        self._titled = True
        self._prefix = ""

    def open(self):
        from Screens.ChoiceBox import ChoiceBox

        view = self.controller.view()
        lines = [view["header"], view["title"]] + ([view["text"]] if view["text"] else [])
        rows = view["rows"]
        choices = [(row["label"], row["id"]) for row in rows]
        # An empty key is the one every image reads as "none".
        keys = [self.controller.key_of(row["id"]) or "" for row in rows]
        ids = [row["id"] for row in rows]
        selection = ids.index(view["initial"]) if view["initial"] in ids else 0
        self.dialog = self.session.openWithCallback(
            self._picked, ChoiceBox, title="\n".join(lines), list=choices, keys=keys,
            selection=selection,
        )
        self._prefix = "" if self._header_in_text(view["header"]) else view["header"] + " - "
        self.countdown(view["countdown"])

    def _header_in_text(self, header):
        try:
            label = self.dialog["text"]
            text = label.getText() if hasattr(label, "getText") else getattr(label, "text", "")
        except Exception:
            return False
        return str(text or "").startswith(header)

    def _picked(self, choice=None):
        controller = self.controller
        # Closed already: an answer made here must not close it a second time.
        self.dialog = None
        try:
            if controller.ended is None:
                ident = None
                if isinstance(choice, (tuple, list)) and len(choice) >= 2:
                    ident = choice[1]
                if ident in controller.ids:
                    controller.key_ok(ident)
                else:
                    controller.key_cancel()
        except Exception:
            LOG.exception("the image's list could not answer")
        finally:
            self.callback()

    def countdown(self, text):
        if not self._titled or self.dialog is None:
            return
        try:
            self.dialog.setTitle(self._prefix + text)
        except Exception:
            self._titled = False
            LOG.info("the image's list takes no title; it shows no countdown")

    def close_list(self):
        dialog = self.dialog
        if dialog is None:
            return
        try:
            dialog.close()
        except Exception:
            LOG.exception("the image's list could not be closed")


def open_choicebox(session, controller, callback):
    """Open the image's own list for `controller`. None when it is open, otherwise why not.

    A `ChoiceBox` that raises inside `session.open` has taken the execution from the dialog
    under it; that dialog is given it back, as after a styled screen that fails there.
    """
    adapter = ChoiceBoxList(session, controller, callback)
    controller.screen = adapter
    executing = getattr(session, "in_exec", None)
    try:
        adapter.open()
    except Exception as error:
        LOG.exception("the image's list could not be opened for a list of choices")
        controller.screen = None
        styledlist._restore_executing(session, executing)
        return f"{type(error).__name__}: {error}"
    return None


def choicebox_refusal(session):
    """None when the image's `ChoiceBox` builds - once, on trial, off the dialog stack."""
    refusal = styledlist.fallback_refusal()
    if refusal is not None:
        return refusal
    try:
        from Screens.ChoiceBox import ChoiceBox

        trial = session.instantiateDialog(ChoiceBox, title=header_text(),
                                          list=[(header_text(), "a")], keys=[""], selection=0)
        if trial is None:
            return "the image returned no ChoiceBox"
        session.deleteDialog(trial)
    except Exception as error:
        LOG.exception("the image's choice list could not be built")
        return f"{type(error).__name__}: {error}"
    return None


# -------------------------------------------------------------------- standby --


def receiver_in_standby():
    """In standby, or asking whether to shut down or restart - the toast's rule."""
    try:
        import Screens.Standby as standby
    except Exception as error:
        missing("Screens.Standby", error)
        return False
    return (
        getattr(standby, "inStandby", None) is not None
        or bool(getattr(standby, "inTryQuitMainloop", False))
    )


def _acknowledged(info):
    """Whether the broker has the publish: paho's PUBACK. A publish that failed counts as done."""
    try:
        return bool(info.is_published())
    except Exception:
        return True


# What the trial build at start draws: two rows, an icon, keys and a text.
SAMPLE = {
    "title": "Home Assistant", "text": "-", "timeout": DEFAULT_TIMEOUT, "default": "a",
    "rows": [{"id": "a", "label": "A", "key": "red", "icon": NEUTRAL_ICON},
             {"id": "b", "label": "B", "key": "1", "icon": None}],
}


# ---------------------------------------------------------------- the publisher --


class ListPublisher(Publisher):
    """The capability `list`: one open list at a time, its countdown, its one answer.

    It publishes no state of its own - `list_answer` is an event - so its snapshot is empty. It
    is a publisher because the registry gives it what it needs: it starts with a session, it
    names itself in `capabilities` when a screen can be built, and it is stopped on every path
    that stops the bridge.
    """

    name = CAPABILITY

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self.screen_kind = None
        self.screen_reason = None
        self.clock = time.monotonic
        # The list on the screen, and the one waiting for the screen.
        self.current = None
        self.waiting = None
        self._tick = Ticker(self._tick_fired, "list countdown")
        self._retry = Ticker(self._retry_fired, "list open")
        self._retrying = False
        self._retries = 0
        self._counter = None
        self._skin_hooked = False
        self._unknown_said = False
        # The answers handed to the client and not acknowledged yet.
        self._unacknowledged = []

    # -------------------------------------------------------------- lifecycle --

    def start(self):
        if self.session is None:
            return False
        if enigma_attribute("eTimer") is None:
            # A list always has a timeout; without a timer there would be none.
            LOG.warning("this image has no eTimer; lists of choices are not offered")
            return False
        self.screen_kind, self.screen_reason = self._probe()
        if self.screen_kind is None:
            return False
        self._counter = self._bind_counter()
        self._hook_skin_reload()
        LOG.info("lists of choices can be shown (screen %s)", self.screen_kind)
        return True

    def _probe(self):
        """`(screen kind, why not styled)` on this image now, or `(None, None)`."""
        styled = styledlist.styled_refusal()
        if styled is None:
            styled = self._trial_build()
        if styled is None:
            return STYLED, None
        fallback = choicebox_refusal(self.session)
        if fallback is None:
            LOG.warning("the styled list screen cannot be built here (%s); the image's own "
                        "choice list is used", styled)
            return CHOICEBOX, styled
        LOG.warning("no list screen can be built on this image: %s; %s", styled, fallback)
        return None, None

    def _trial_build(self):
        """None when the styled screen builds with this skin, otherwise why not."""
        session = self.session
        try:
            controller = ListController(self, dict(SAMPLE))
            shape = desktop_shape(controller.spec)
            if shape is None:
                return "the image will not say how big its desktop is"
            trial = session.instantiateDialog(screen_class(), shape, _Trial(controller))
            if trial is None:
                return "the image returned no screen"
            session.deleteDialog(trial)
        except Exception as error:
            LOG.exception("the styled list screen could not be built")
            return f"{type(error).__name__}: {error}"
        return None

    def claimed(self):
        return self.screen_kind is not None

    def info(self):
        """`info.list`: which screen this image shows a list with."""
        return {"screen": self.screen_kind}

    def before_stop(self):
        """The plugin is about to stop while its session is still up: answer now, `closed`."""
        self._close_all(CLOSED)

    def stop(self):
        try:
            self._unhook_skin_reload()
            self._unbind_counter()
            self._close_all(CLOSED)
            self._tick.stop()
            self._stop_retry()
        except Exception:
            LOG.exception("letting go of the lists of choices raised; the receiver is unaffected")

    def _close_all(self, ending):
        for controller in (self.waiting, self.current):
            if controller is not None:
                self.end(controller, ending)

    # ------------------------------------------------------------ the command --

    def request(self, text, command_id=None):
        """`cmd/list`: None when the list is shown or waits for the screen, else the refusal."""
        try:
            spec, unknown = parse_list(text)
        except InvalidList as error:
            return Refusal(str(error), INVALID)
        if unknown and not self._unknown_said:
            self._unknown_said = True
            LOG.info("cmd/list: %d member(s) this plugin does not know were ignored (said once "
                     "a run)", unknown)
        if not self.claimed():
            return Refusal(NOT_AVAILABLE, NOT_AVAILABLE_REASON)
        if receiver_in_standby():
            return Refusal(STANDBY, STANDBY_REASON)
        controller = ListController(self, spec, command_id)
        # A newer list replaces the one on screen, and one still waiting for the screen.
        for older in (self.waiting, self.current):
            if older is not None:
                self.end(older, REPLACED)
        LOG.info("list %s: %d row(s), %d s%s", controller.label(), len(spec["rows"]),
                 spec["timeout"], ", with a default" if spec["default"] else "")
        self.waiting = controller
        # A list that waits counts its own turns, never those of the list it replaced.
        self._stop_retry()
        self._retries = 0
        return self._open_waiting()

    # ----------------------------------------------------------- the screens --

    def _open_waiting(self):
        """Open the waiting list if the session takes a screen now. The refusal, or None."""
        controller = self.waiting
        if controller is None:
            self._stop_retry()
            return None
        session = self.session
        if session is None:
            self.waiting = None
            controller.ended = NOT_SHOWN_MARK
            return Refusal(NOT_SHOWN, NOT_SHOWN_REASON)
        if self.current is not None or not _can_open(session):
            # The list it replaced is still closing, or another screen is: the next turn.
            if not self._retrying:
                self._retries = 0
                if not self._retry.start(OPEN_RETRY_MILLISECONDS):
                    return self._not_shown(controller, "the image gives no timer to wait with")
                self._retrying = True
            return None
        self._stop_retry()
        self.waiting = None
        refusal = self._show(controller)
        if refusal is not None:
            return self._not_shown(controller, refusal)
        return None

    def _not_shown(self, controller, why):
        LOG.warning("list %s could not be shown: %s", controller.label(), why)
        if self.waiting is controller:
            self.waiting = None
        controller.ended = NOT_SHOWN_MARK
        return Refusal(NOT_SHOWN, NOT_SHOWN_REASON)

    def _stop_retry(self):
        self._retrying = False
        self._retry.stop()

    def _retry_fired(self):
        controller = self.waiting
        if controller is None:
            self._stop_retry()
            return
        self._retries += 1
        if self._retries >= OPEN_RETRY_LIMIT and (self.current is not None
                                                  or not _can_open(self.session)):
            self._stop_retry()
            refusal = self._not_shown(controller, "the receiver was between two screens for "
                                      "too long")
            self._report(controller, refusal)
            return
        refusal = self._open_waiting()
        if refusal is not None:
            self._report(controller, refusal)

    def _report(self, controller, refusal):
        """A refusal made after the command returned, with that command's id."""
        if self.bridge is not None:
            self.bridge.publish_last_error(COMMAND, refusal, command_id=controller.command_id)

    def _show(self, controller):
        """Put `controller` on the screen. None when it is there, otherwise why not."""
        session = self.session
        before = (self.screen_kind, self.screen_reason)
        under = getattr(session, "current_dialog", None)
        controller.under = under
        controller.suspend = _suspend_of(under)
        refusal = None
        opened = False
        callback = self._closed_callback(controller)
        if self.screen_kind == STYLED:
            refusal = open_styled(session, controller, self.value("menu_transparency") or 0,
                                  callback)
            if refusal is None:
                opened = True
            else:
                LOG.warning("the styled list screen could not be built (%s); the image's own "
                            "choice list is used", refusal)
                self.screen_kind = CHOICEBOX
                self.screen_reason = refusal
        if not opened and self.screen_kind == CHOICEBOX:
            fallback = styledlist.fallback_refusal()
            refusal = fallback if fallback is not None else open_choicebox(session, controller,
                                                                           callback)
            opened = refusal is None
        if (self.screen_kind, self.screen_reason) != before and self.bridge is not None:
            self.bridge.announce_capabilities()
        if not opened:
            return refusal or "no screen"
        # Shown: the countdown starts now, and until now showed the whole timeout.
        controller.deadline = self.clock() + controller.spec["timeout"]
        self.current = controller
        if not self._arm(controller):
            LOG.warning("list %s: no timer for its countdown; closed", controller.label())
            self.end(controller, CLOSED)
        return None

    def _closed_callback(self, controller):
        def closed(*_answer):
            try:
                self.screen_gone(controller)
            except Exception:
                LOG.exception("the list's screen closing raised")

        return closed

    def screen_gone(self, controller):
        """A list's screen has closed - by its own answer, or by something else - and is off.

        Asked twice for the plugin's own screen - by its `onClose`, and by the session's
        callback once the screen under it executes again - and the second finds nothing to do
        but open a list that is waiting, which only the second can.
        """
        controller.screen = None
        if controller.ended is None:
            # Closed by something other than its answer: the image, another plugin.
            self.end(controller, CLOSED)
        if self.current is controller:
            self.current = None
        waiting = self.waiting
        if waiting is not None:
            refusal = self._open_waiting()
            if refusal is not None:
                self._report(waiting, refusal)

    # --------------------------------------------------------- the countdown --

    def _arm(self, controller):
        """The next tick, on the next whole second of the countdown. False without a timer."""
        left = controller.deadline - self.clock()
        fraction = left - math.floor(left)
        wait = int(round(fraction * 1000)) if fraction > 0.001 else 1000
        return self._tick.start(max(1, wait), True)

    def _tick_fired(self):
        controller = self.current
        if controller is None or controller.ended is not None:
            return
        if controller.deadline - self.clock() <= 0.0005:
            self.end(controller, TIMEOUT, controller.spec["default"])
            return
        screen = controller.screen
        if screen is not None:
            try:
                screen.countdown(controller.countdown())
            except Exception:
                LOG.exception("the list's countdown could not be drawn")
        if not self._arm(controller):
            LOG.warning("list %s: the countdown's timer stopped; closed", controller.label())
            self.end(controller, CLOSED)

    # ------------------------------------------------------------ the answer --

    def end(self, controller, ending, row=None):
        """End `controller`'s list with its one answer. False when it had ended already.

        Every way a list ends comes here, and the first one wins: a timeout and an OK in the
        same turn, an EXIT on a list that was replaced, a pick on a list closed for standby -
        the second finds the list ended and does nothing.
        """
        if controller.ended is not None:
            return False
        controller.ended = ending
        if self.waiting is controller:
            self.waiting = None
        if self.current is controller:
            self.current = None
            self._tick.stop()
        self._answer(controller, ending, row if ending in (PICKED, TIMEOUT) else None)
        screen = controller.screen
        if screen is not None:
            screen.close_list()
        return True

    def _answer(self, controller, ending, row):
        payload = {"ending": ending, "row": row, "ts": int(time.time())}
        if controller.command_id is not None:
            payload["id"] = controller.command_id
        bridge = self.bridge
        info = bridge.publish_event(ANSWER_SUFFIX, payload, qos=ANSWER_QOS) \
            if bridge is not None else None
        if info is None or getattr(info, "rc", 0):
            # No session, or the socket was already gone: the client would keep a QoS 1
            # message for the next connect, and an answer must never arrive late.
            if info is not None:
                bridge.withdraw_event(info)
            LOG.warning("list %s ended %s; the answer was not sent", controller.label(), ending)
            return
        self._unacknowledged = [
            sent for sent in self._unacknowledged if not _acknowledged(sent)
        ] + [info]
        LOG.info("list %s ended: %s", controller.label(), ending)

    def connection_changed(self):
        """The broker connection came or went: an answer it had not taken is never sent late.

        The client drops the same answers on its network thread at the next connect, before
        paho sends anything again (`mqttclient.MqttClient.publish`, `transient`), which holds
        even when this runs late.
        """
        if self.bridge is not None and not self.bridge.connected:
            for info in self._unacknowledged:
                if not _acknowledged(info) and self.bridge.withdraw_event(info):
                    LOG.info("dropped a list answer the broker had not acknowledged")
            self._unacknowledged = []

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
        """The receiver went to standby: the list on screen, or waiting for it, ends `closed`."""
        try:
            self._close_all(CLOSED)
        except Exception:
            LOG.exception("closing the list for standby raised")

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
        """A new skin: which screen a list is shown with is asked again, the toast's rule."""
        try:
            before = (self.screen_kind, self.screen_reason)
            self.screen_kind, self.screen_reason = self._probe()
            if self.screen_kind is None:
                LOG.warning("no list screen can be built after the skin reload; the capability "
                            "list is taken back")
            if (self.screen_kind, self.screen_reason) != before and self.bridge is not None:
                self.bridge.announce_capabilities()
        except Exception:
            LOG.exception("asking the list screens again after a skin reload raised")
