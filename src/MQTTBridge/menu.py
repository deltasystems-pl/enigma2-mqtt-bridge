"""The receiver menu: a "Home Assistant" menu on the television that Home Assistant keeps current.

**The receiver never learns what a row does.** The companion integration publishes the menu,
retained, on `enigma2mqtt/menu/<node>` - groups and rows, each row a control bound to something in
Home Assistant, with its current value and the text to show. The receiver draws it, and when the
viewer acts on a row it publishes one message on `<base>/<node>/menu_action`: the row's id, what
was done to it (`press`, `toggle`, `set`) and, for `set`, the value. Home Assistant performs the
action and publishes the menu again with the new state. There is no service call, no entity id and
no domain anywhere on the receiver, so the only power a `menu_action` carries is what the user put
into the menu (ADR-0020, docs/TOPICS.md).

Three parts live here:

- **The payload** (`parse_menu`): the contract's shape, bounds and types, checked whole. An invalid
  payload is ignored with one line in the log and the menu before it stays. Text is cut again at
  the contract's lengths and loses every backslash and line break before it reaches the screen,
  for the toast's reason (`toast.strip_backslashes`).
- **The navigation** (`MenuController`): one controller for each time the menu is opened, the
  same for the styled screen and for the image's own list (`styledlist.py`). The top list holds
  groups and rows; OK on a group opens its rows, one level deep. A selector opens its options; a
  slider moves a local value with LEFT and RIGHT that is sent only on OK; an input field opens the
  image's keyboard. EXIT discards whatever was not applied, and goes back.
- **The publisher** (`MenuPublisher`): the capability `menu`, the two subscriptions, Home
  Assistant's reachability, the pending marks, the hotkey and `info.menu`.

**Greyed (D14).** With no broker connection, or with Home Assistant's own status topic saying
`offline`, the menu stays on screen as it last was, greyed, with a notice, and nothing in it can be
acted on. A menu that never came, or an empty payload, is "no menu yet" - not greyed, and nothing to
act on.

**Pending (D54).** After OK on a toggle, a slider or a selector the row shows a pending mark until
the menu topic carries a different value for it. Five seconds without one and the row shows its
real value again, and the footer says Home Assistant changed nothing. The comparison is of the
row's `value`, never of `rev`.

Everything here runs on enigma2's main loop and nothing raises into it: a failure is a line in the
log and a menu that does less.
"""

import json
import math
import re
import secrets
import time
import unicodedata

from .enigma2 import Ticker
from .i18n import _
from .log import get_logger
from .publisher import Publisher
from .toast import strip_backslashes

LOG = get_logger("menu")

CAPABILITY = "menu"

# The topic the integration owns, outside the node's tree like `enigma2mqtt/integration/<node>`.
MENU_TOPIC_PREFIX = "enigma2mqtt/menu/"
# Home Assistant's birth and last will, under the discovery prefix the plugin already has.
STATUS_SUFFIX = "/status"
# The viewer's action, in the node's tree. An event: QoS 1, never retained.
ACTION_SUFFIX = "menu_action"
ACTION_QOS = 1
SUBSCRIBE_QOS = 1

MENU_VERSION = 1
DEFAULT_TITLE = "Home Assistant"

# The contract's bounds (D24).
MAX_PAYLOAD_BYTES = 65536
MAX_ITEMS = 24
MAX_GROUPS = 8
MAX_ROWS = 24
MAX_OPTIONS = 24
MAX_SLIDER_STEPS = 200
TITLE_LIMIT = 60
LABEL_LIMIT = 60
STATE_LIMIT = 24
OPTION_LIMIT = 60
UNIT_LIMIT = 8
MAX_LENGTH_RANGE = (1, 255)

PENDING_SECONDS = 5

GROUP = "group"
BUTTON = "button"
TOGGLE = "toggle"
INDICATOR = "indicator"
SELECTOR = "selector"
SLIDER = "slider"
INPUT = "input"
ROW_TYPES = (BUTTON, TOGGLE, INDICATOR, SELECTOR, SLIDER, INPUT)
# What OK sends for each type. An indicator, and a type this plugin does not know, send nothing.
ACTIONS = {BUTTON: "press", TOGGLE: "toggle", SELECTOR: "set", SLIDER: "set", INPUT: "set"}
# The rows whose OK leaves a pending mark (D54).
PENDING_TYPES = (TOGGLE, SELECTOR, SLIDER)

GREEN = "green"
RED = "red"
COLOURS = (GREEN, RED)

ONLINE = "online"
OFFLINE = "offline"

# Where the image's media player plugin lives (`Plugins/Extensions/MediaPlayer/plugin.py`).
MEDIA_PLAYER_MODULE = "Plugins.Extensions.MediaPlayer"

# Why the menu is greyed, when it is.
GREY_BROKER = "broker"
GREY_HOME_ASSISTANT = "home_assistant"

# The icon names: the contract. The integration offers exactly these; any other name, or none,
# is drawn with `neutral`. Each is a PNG in `icons/`, rendered from Material Design Icons by
# `tools/render-icons.py` (assets/icons/manifest.json says which icon each name is).
NEUTRAL_ICON = "neutral"
ICON_NAMES = (
    "neutral", "lamp", "ceiling-light", "blinds", "scene", "script", "power", "fan", "lock",
    "radiator", "thermometer", "tv", "speaker", "door", "gate", "garage", "alarm", "camera",
    "sofa", "bed", "kitchen", "garden", "movie", "note", "home",
)

_ROW_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_ACTION_ID_BYTES = 6


class InvalidMenu(ValueError):
    """The payload is not a menu of this contract; the sentence says where it is not."""


class UnknownVersion(InvalidMenu):
    """A menu of a `v` this plugin does not know: a newer contract, not a broken payload."""

    def __init__(self, version):
        InvalidMenu.__init__(self, f"a menu of version {version!r}, which this plugin does "
                             "not know")
        self.version = version


# ------------------------------------------------------------------- the text --


def clean_text(value, limit):
    """Text as a row may show it: no backslash, no line break or control, at most `limit`.

    The backslash goes for the toast's reason - enigma2's renderer reads `\\c` and `\\n` as
    escapes, after reordering right-to-left text, where no pattern over the string finds them.
    A row is one line, so every control character and line separator becomes a space.
    """
    text = strip_backslashes(str(value))
    text = "".join(
        " " if unicodedata.category(character) in ("Cc", "Zl", "Zp") else character
        for character in text
    )
    return text[:limit]


def format_number(value):
    """A slider's number as a person reads it: no `.0`, no binary noise."""
    if value is None:
        return ""
    if isinstance(value, int) or float(value).is_integer():
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- the payload --


def _is_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _text(raw, name, limit, required=False, default=None):
    if raw is None:
        if required:
            raise InvalidMenu(name + " is missing")
        return default
    if not isinstance(raw, str):
        raise InvalidMenu(name + " is not text")
    return clean_text(raw, limit)


def _icon(raw):
    return raw if isinstance(raw, str) and raw in ICON_NAMES else NEUTRAL_ICON


def _row_id(raw, seen, where):
    if not isinstance(raw, str) or not _ROW_ID.fullmatch(raw):
        raise InvalidMenu(where + ": an id must be 1-32 of A-Z, a-z, 0-9, _ and -")
    if raw in seen:
        raise InvalidMenu(where + ": the id " + raw + " is not unique")
    seen.add(raw)
    return raw


def _decimals(number):
    text = repr(float(number))
    if "e" in text or "E" in text:
        return 6
    return len(text.partition(".")[2].rstrip("0"))


def _parse_row(raw, seen, where):
    if not isinstance(raw, dict):
        raise InvalidMenu(where + " is not an object")
    kind = raw.get("type")
    if not isinstance(kind, str):
        raise InvalidMenu(where + ": type is not text")
    ident = _row_id(raw.get("id"), seen, where)
    where = where + " (" + ident + ")"
    available = raw.get("available", True)
    if not isinstance(available, bool):
        raise InvalidMenu(where + ": available is not true or false")
    colour = raw.get("color")
    row = {
        "type": kind,
        "id": ident,
        "label": _text(raw.get("label"), where + ": label", LABEL_LIMIT, default=""),
        "icon": _icon(raw.get("icon")),
        "state": _text(raw.get("state"), where + ": state", STATE_LIMIT),
        # An enumeration of the contract, and open: a colour this plugin does not know is none.
        "color": colour if colour in COLOURS else None,
        "available": available,
        "value": None,
    }
    value = raw.get("value")
    if kind in (TOGGLE, INDICATOR):
        if value is not None and not isinstance(value, bool):
            raise InvalidMenu(where + ": value is not true, false or null")
        row["value"] = value
    elif kind == SELECTOR:
        options = raw.get("options")
        if not isinstance(options, list) or len(options) > MAX_OPTIONS:
            raise InvalidMenu(where + f": options is not a list of at most {MAX_OPTIONS}")
        if not all(isinstance(option, str) for option in options):
            raise InvalidMenu(where + ": an option is not text")
        if value is not None and value not in options:
            raise InvalidMenu(where + ": value is not one of the options")
        # What is shown is cut; what is sent back is the option exactly as it came.
        row["options"] = [(option, clean_text(option, OPTION_LIMIT)) for option in options]
        row["value"] = value
    elif kind == SLIDER:
        minimum, maximum, step = raw.get("min"), raw.get("max"), raw.get("step")
        if not (_is_number(minimum) and _is_number(maximum) and _is_number(step)):
            raise InvalidMenu(where + ": min, max and step must be numbers")
        if step <= 0 or maximum < minimum:
            raise InvalidMenu(where + ": step must be above 0 and max at least min")
        if (maximum - minimum) / step > MAX_SLIDER_STEPS + 1e-9:
            raise InvalidMenu(where + f": more than {MAX_SLIDER_STEPS} steps between min "
                              "and max")
        if value is not None and not _is_number(value):
            raise InvalidMenu(where + ": value is not a number or null")
        row.update({
            "min": minimum, "max": maximum, "step": step,
            "unit": _text(raw.get("unit"), where + ": unit", UNIT_LIMIT),
            "value": value,
        })
    elif kind == INPUT:
        length = raw.get("max_length")
        low, high = MAX_LENGTH_RANGE
        if not isinstance(length, int) or isinstance(length, bool) or not low <= length <= high:
            raise InvalidMenu(where + f": max_length must be {low}-{high}")
        if value is not None and not isinstance(value, str):
            raise InvalidMenu(where + ": value is not text or null")
        row["max_length"] = length
        row["value"] = value
    # A button has no value; a type this plugin does not know is shown and cannot be acted on.
    return row


def parse_menu(payload):
    """The menu a payload holds: None for "no menu" (an empty payload), else the menu.

    Raises InvalidMenu - with the place it went wrong - for anything else, and
    `UnknownVersion` for a `v` this plugin does not know, which is not an error in the payload
    but a newer contract than this plugin's.
    """
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise InvalidMenu(f"the payload is {len(payload)} bytes, over "
                              f"{MAX_PAYLOAD_BYTES}")
        if not payload.strip():
            return None
        try:
            text = bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidMenu("the payload is not UTF-8") from None
    else:
        text = str(payload)
        if not text.strip():
            return None
    try:
        raw = json.loads(text)
    except ValueError:
        raise InvalidMenu("the payload is not JSON") from None
    if not isinstance(raw, dict):
        raise InvalidMenu("the payload is not a JSON object")
    version = raw.get("v")
    if not isinstance(version, int) or isinstance(version, bool):
        raise InvalidMenu("v is not a number")
    if version != MENU_VERSION:
        raise UnknownVersion(version)
    revision = raw.get("rev")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise InvalidMenu("rev is not an integer")
    items = raw.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise InvalidMenu(f"items is not a list of at most {MAX_ITEMS}")
    title = _text(raw.get("title"), "title", TITLE_LIMIT, default=DEFAULT_TITLE)
    seen = set()
    parsed = []
    groups = 0
    for position, item in enumerate(items):
        where = f"item {position + 1}"
        if isinstance(item, dict) and item.get("type") == GROUP:
            groups += 1
            if groups > MAX_GROUPS:
                raise InvalidMenu(f"more than {MAX_GROUPS} groups")
            ident = _row_id(item.get("id"), seen, where)
            rows = item.get("rows")
            if not isinstance(rows, list) or len(rows) > MAX_ROWS:
                raise InvalidMenu(where + f": rows is not a list of at most {MAX_ROWS}")
            members = []
            for index, row in enumerate(rows):
                inner = where + f" row {index + 1}"
                if isinstance(row, dict) and row.get("type") == GROUP:
                    raise InvalidMenu(inner + ": a group holds no group")
                members.append(_parse_row(row, seen, inner))
            parsed.append({
                "type": GROUP,
                "id": ident,
                "label": _text(item.get("label"), where + ": label", LABEL_LIMIT, default=""),
                "icon": _icon(item.get("icon")),
                "rows": members,
            })
        else:
            parsed.append(_parse_row(item, seen, where))
    index = {}
    for item in parsed:
        if item["type"] == GROUP:
            for row in item["rows"]:
                index[row["id"]] = row
        else:
            index[item["id"]] = item
    return {"v": version, "rev": revision, "title": title or DEFAULT_TITLE, "items": parsed,
            "rows": index}


# ----------------------------------------------------------------- the slider --


def slider_count(row):
    """How many steps above `min` the slider has: its values are min + k*step, k = 0..count."""
    return int(math.floor((row["max"] - row["min"]) / row["step"] + 1e-9))


def slider_value(row, index):
    """The slider's value at step `index`, an int when min and step are whole numbers."""
    value = row["min"] + index * row["step"]
    if float(row["min"]).is_integer() and float(row["step"]).is_integer():
        return int(round(value))
    return round(value, max(_decimals(row["min"]), _decimals(row["step"])))


def slider_index(row, value):
    """The step nearest `value`, within the slider; `min` when there is no value."""
    if value is None:
        return 0
    index = int(round((value - row["min"]) / row["step"]))
    return max(0, min(slider_count(row), index))


def slider_text(row, value):
    unit = row.get("unit")
    return format_number(value) + (" " + unit if unit else "")


# ------------------------------------------------------------- the navigation --

TOP = "top"
IN_GROUP = "group"
OPTIONS = "options"
VALUES = "values"

# What the footer says. Keys, not sentences: the sentence is built in the household's language.
NOTICE_SENT = "sent"
NOTICE_NOTHING_CHANGED = "nothing_changed"
NOTICE_NOT_SENT = "not_sent"
NOTICE_READ_ONLY = "read_only"
NOTICE_UNAVAILABLE = "unavailable"
NOTICE_USE_ARROWS = "use_arrows"


def notice_text(key):
    texts = {
        NOTICE_SENT: _("Sent to Home Assistant."),
        NOTICE_NOTHING_CHANGED: _("Home Assistant changed nothing."),
        NOTICE_NOT_SENT: _("Could not send this to Home Assistant."),
        NOTICE_READ_ONLY: _("This row only shows a state."),
        NOTICE_UNAVAILABLE: _("This is unavailable in Home Assistant."),
        NOTICE_USE_ARROWS: _("Use LEFT and RIGHT to change the value, then OK."),
        GREY_BROKER: _("No connection to the MQTT broker. Showing the last menu."),
        GREY_HOME_ASSISTANT: _("Home Assistant is not reachable. Showing the last menu."),
    }
    return texts.get(key, "")


class MenuController:
    """What is on the screen and what a key does - for one opening of the menu.

    The screen (`styledlist.py`) asks `view()` for what to draw and hands every key to one of
    the `key_*` methods with the id of the selected row; the controller answers by asking the
    screen to draw again, to open the keyboard, or to close. It holds no state of its own about
    the menu's content: that is the publisher's, so a menu that changes while the screen is open
    is drawn as it is now.

    `adjustable` is False for the image's own list, which has no LEFT and RIGHT to give: a slider
    then opens a list of its values, as a selector opens its options.
    """

    def __init__(self, publisher, adjustable=True):
        self.publisher = publisher
        self.adjustable = adjustable
        self.level = TOP
        self.group = None
        # The selector or slider whose options or values are listed.
        self.chooser = None
        # (row id, the value moved to) of the slider being moved with LEFT and RIGHT.
        self.slider = None
        self.notice = None
        self.closed = False
        self.screen = None
        # The id that was selected on each level, so going back lands where one left.
        self._remembered = {}

    # ------------------------------------------------------------- the content --

    @property
    def menu(self):
        return self.publisher.menu

    def _group(self):
        menu = self.menu
        if menu is None or self.group is None:
            return None
        for item in menu["items"]:
            if item["type"] == GROUP and item["id"] == self.group:
                return item
        return None

    def _row(self, ident):
        menu = self.menu
        return None if menu is None else menu["rows"].get(ident)

    def _settle(self):
        """Step back out of what the menu no longer has."""
        if self.level in (OPTIONS, VALUES):
            row = self._row(self.chooser)
            wanted = SELECTOR if self.level == OPTIONS else SLIDER
            if row is None or row["type"] != wanted or not self.publisher.actionable(row):
                self._leave_chooser()
        if self.level == IN_GROUP and self._group() is None:
            self.level = TOP
            self.group = None
        if self.slider is not None:
            row = self._row(self.slider[0])
            if row is None or row["type"] != SLIDER or not self.publisher.actionable(row):
                self.slider = None
            else:
                low, high = row["min"], row["max"]
                self.slider = (self.slider[0], max(low, min(high, self.slider[1])))

    def _leave_chooser(self):
        row = self._row(self.chooser) if self.chooser else None
        self.level = IN_GROUP if self.group is not None and self._group() is not None else TOP
        if self.level == TOP:
            self.group = None
        self.chooser = None
        return row

    def entries(self):
        """The rows of the current level: the contract's dicts, or the options as rows."""
        menu = self.menu
        if menu is None:
            return []
        if self.level == IN_GROUP:
            group = self._group()
            return list(group["rows"]) if group is not None else []
        if self.level == OPTIONS:
            row = self._row(self.chooser)
            return [
                {"type": "option", "id": f"o{index}", "label": shown, "value": raw,
                 "current": raw == row["value"]}
                for index, (raw, shown) in enumerate(row["options"])
            ]
        if self.level == VALUES:
            row = self._row(self.chooser)
            current = slider_index(row, row["value"]) if row["value"] is not None else None
            return [
                {"type": "option", "id": f"v{index}",
                 "label": slider_text(row, slider_value(row, index)),
                 "value": slider_value(row, index), "current": index == current}
                for index in range(slider_count(row) + 1)
            ]
        return list(menu["items"])

    # ---------------------------------------------------------------- the view --

    def title(self):
        menu = self.menu
        title = menu["title"] if menu is not None else DEFAULT_TITLE
        parts = [title]
        group = self._group() if self.level != TOP else None
        if group is not None:
            parts.append(group["label"])
        if self.level in (OPTIONS, VALUES):
            row = self._row(self.chooser)
            if row is not None:
                parts.append(row["label"])
        return " / ".join(part for part in parts if part)

    def view(self):
        """Everything the screen draws, as plain data."""
        publisher = self.publisher
        greyed = publisher.greyed()
        rows = [self._row_view(entry, greyed) for entry in self.entries()]
        if greyed:
            notice = notice_text(greyed)
        elif self.menu is None:
            notice = _("There is no menu from Home Assistant yet.")
        elif not rows:
            # A group Home Assistant sent without rows: a line that says so, not an error.
            notice = _("There are no controls here.")
        else:
            notice = notice_text(self.notice) if self.notice else ""
        initial = self._remembered.get(self._level_key())
        if initial is None:
            initial = next((entry["id"] for entry in self.entries() if entry.get("current")),
                           None)
        return {
            "title": self.title(),
            "marker": _("from Home Assistant"),
            "rows": rows,
            "notice": notice,
            "legend": self.legend(),
            "greyed": bool(greyed),
            # Which list this is: a new one starts on `initial`, the same one keeps its row.
            "level": ":".join(str(part) for part in self._level_key()),
            "initial": initial,
        }

    def legend(self):
        if self.level in (OPTIONS, VALUES):
            return _("OK apply   EXIT discard")
        if self.slider is not None:
            return _("LEFT/RIGHT change   OK apply   EXIT discard")
        if self.adjustable and any(
            entry.get("type") == SLIDER for entry in self.entries()
        ):
            return _("OK select   LEFT/RIGHT adjust   EXIT back")
        return _("OK select   EXIT back")

    def _row_view(self, entry, greyed):
        kind = entry.get("type")
        if kind == "option":
            return {"id": entry["id"], "icon": None, "label": entry["label"],
                    "state": _("current") if entry["current"] else "",
                    "colour": GREEN if entry["current"] else None,
                    "dim": bool(greyed), "pending": False}
        if kind == GROUP:
            return {"id": entry["id"], "icon": entry["icon"], "label": entry["label"],
                    "state": ">", "colour": None, "dim": bool(greyed), "pending": False}
        pending = self.publisher.pending_for(entry["id"])
        state = entry.get("state") or ""
        colour = entry.get("color") if kind in (TOGGLE, INDICATOR) else None
        if self.slider is not None and self.slider[0] == entry["id"]:
            state = "< " + slider_text(entry, self.slider[1]) + " >"
            colour = None
        elif pending:
            state = self._pending_text(entry, pending)
            colour = None
        return {
            "id": entry["id"],
            "icon": entry.get("icon"),
            "label": entry.get("label", ""),
            "state": state,
            "colour": colour,
            "dim": bool(greyed) or not entry.get("available", True),
            "pending": bool(pending),
        }

    @staticmethod
    def _pending_text(row, pending):
        sent = pending.get("sent")
        if row["type"] == SLIDER and sent is not None:
            return slider_text(row, sent) + " \u2026"
        if row["type"] == SELECTOR and sent is not None:
            shown = dict(row.get("options") or []).get(sent, sent)
            return clean_text(shown, STATE_LIMIT - 2) + " \u2026"
        return "\u2026"

    def _level_key(self):
        return (self.level, self.group, self.chooser)

    # ---------------------------------------------------------- the screen link --

    def attach(self, screen):
        self.screen = screen

    def refresh(self):
        """Draw again: the menu, the reachability or a pending mark changed."""
        if self.closed:
            return
        self._settle()
        if self.screen is not None:
            try:
                self.screen.render(self.view())
            except Exception:
                LOG.exception("the menu could not be drawn again")

    def _remember(self, selected):
        if selected is not None:
            self._remembered[self._level_key()] = selected

    def _close(self):
        self.closed = True
        self.slider = None
        self.publisher.controller_closed(self)
        if self.screen is not None:
            self.screen.close_menu()

    # ----------------------------------------------------------------- the keys --

    def key_ok(self, selected):
        self.notice = None
        entries = {entry["id"]: entry for entry in self.entries()}
        entry = entries.get(selected)
        if entry is None:
            self.refresh()
            return
        self._remember(selected)
        if self.level in (OPTIONS, VALUES):
            row = self._row(self.chooser)
            self._leave_chooser()
            if row is not None and not self.publisher.greyed():
                self._send(row, "set", entry["value"])
            self.refresh()
            return
        if entry["type"] == GROUP:
            self.level = IN_GROUP
            self.group = entry["id"]
            self.slider = None
            self.refresh()
            return
        self._act(entry)
        self.refresh()

    def _act(self, row):
        if self.publisher.greyed():
            return
        kind = row["type"]
        if kind not in ACTIONS:
            self.notice = NOTICE_READ_ONLY
            return
        if not row["available"]:
            self.notice = NOTICE_UNAVAILABLE
            return
        if kind == BUTTON:
            self._send(row, "press", None)
        elif kind == TOGGLE:
            self._send(row, "toggle", None)
        elif kind == SELECTOR:
            if row["options"]:
                self.slider = None
                self.level = OPTIONS
                self.chooser = row["id"]
        elif kind == SLIDER:
            if self.slider is not None and self.slider[0] == row["id"]:
                value = self.slider[1]
                self.slider = None
                self._send(row, "set", value)
            elif self.adjustable:
                self.notice = NOTICE_USE_ARROWS
            else:
                self.level = VALUES
                self.chooser = row["id"]
        elif kind == INPUT:
            self._keyboard(row)

    def _keyboard(self, row):
        if self.screen is None:
            return
        text = row["value"] or ""
        text = text[: row["max_length"]]
        ident = row["id"]

        def typed(answer):
            try:
                self._typed(ident, answer)
            except Exception:
                LOG.exception("the typed text could not be sent")

        self.screen.open_keyboard(row["label"], text, typed)

    def _typed(self, ident, answer):
        if self.closed:
            return
        if answer is None:
            self.refresh()
            return
        row = self._row(ident)
        if row is not None and row["type"] == INPUT and not self.publisher.greyed():
            self._send(row, "set", str(answer)[: row["max_length"]])
        self.refresh()

    def _send(self, row, action, value):
        sent = self.publisher.send(row, action, value)
        if not sent:
            self.notice = NOTICE_NOT_SENT
        elif row["type"] not in PENDING_TYPES:
            self.notice = NOTICE_SENT

    def key_cancel(self, selected=None):
        """EXIT: discard what was not applied, or go back, or close."""
        self.notice = None
        if self.slider is not None:
            self.slider = None
            self.refresh()
            return
        if self.level in (OPTIONS, VALUES):
            self._leave_chooser()
            self.refresh()
            return
        if self.level == IN_GROUP:
            self.level = TOP
            self.group = None
            self.refresh()
            return
        self._close()

    def key_adjust(self, selected, direction):
        """LEFT (-1) or RIGHT (+1) on the selected row. True when it was a slider's."""
        if not self.adjustable or self.level not in (TOP, IN_GROUP):
            return False
        row = self._row(selected)
        if row is None or row["type"] != SLIDER:
            return False
        self.notice = None
        if not self.publisher.actionable(row):
            self.notice = NOTICE_UNAVAILABLE if not row["available"] else None
            self.refresh()
            return True
        if self.slider is not None and self.slider[0] == selected:
            index = slider_index(row, self.slider[1])
        else:
            index = slider_index(row, row["value"])
        index = max(0, min(slider_count(row), index + direction))
        self.slider = (selected, slider_value(row, index))
        self.refresh()
        return True

    def key_number(self, number):
        """1-9 move the selection to the row of that number on this level, and do nothing else.

        OK acts (D66): a digit pressed by mistake must not switch a lamp.
        """
        entries = self.entries()
        if not 1 <= number <= len(entries):
            return None
        ident = entries[number - 1]["id"]
        if self.screen is not None:
            self.screen.select(ident)
        return ident


# ----------------------------------------------------------------- the publisher --


def hotkey_screen(dialog):
    """Whether the hotkey may open the menu over `dialog`: live TV or a player.

    The info bar itself; the image's movie player (`Screens.InfoBar.MoviePlayer`, and the
    players plugins build on it); and the image's media player plugin. Every other screen - a
    list, a menu, the EPG - keeps the key to itself.
    """
    try:
        from Screens.InfoBar import InfoBar
    except Exception:
        return False
    if dialog is getattr(InfoBar, "instance", None):
        return True
    try:
        from Screens.InfoBar import MoviePlayer
    except Exception:
        MoviePlayer = None
    if MoviePlayer is not None and isinstance(dialog, MoviePlayer):
        return True
    return any(
        cls.__name__ == "MediaPlayer" and cls.__module__.startswith(MEDIA_PLAYER_MODULE)
        for cls in type(dialog).__mro__
    )


def _acknowledged(info):
    """Whether the broker has the publish: paho's PUBACK. A publish that failed counts as done."""
    try:
        return bool(info.is_published())
    except Exception:
        return True


class MenuPublisher(Publisher):
    """The capability `menu`: subscriptions, reachability, pending marks, hotkey, screen.

    It publishes no state of its own - the menu is the integration's topic and `menu_action`
    an event - so its snapshot is empty. It is a publisher because the registry gives it what it
    needs: it starts with a session and only with the permission on, it names itself in
    `capabilities` when the screen can be opened, and it is stopped on every path that stops the
    bridge.
    """

    name = CAPABILITY

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self.menu = None
        self.menu_seen = False
        self.ha_status = None
        self.pending = {}
        self.clock = time.monotonic
        self.screen_kind = None
        self.screen_reason = None
        self._controllers = []
        self._ticker = Ticker(self._expire, "menu pending")
        self._versions_logged = set()
        self._hotkey = None
        # The `menu_action`s handed to the client and not acknowledged yet.
        self._unacknowledged = []

    # -------------------------------------------------------------- lifecycle --

    def start(self):
        if not self.value("menu_allowed"):
            self.switched_off = True
            LOG.info("menu_allowed is off; the receiver menu is not offered")
            return False
        if self.session is None:
            return False
        from . import styledlist

        styled = styledlist.styled_refusal()
        fallback = styledlist.fallback_refusal()
        if styled is None:
            self.screen_kind = styledlist.STYLED
        elif fallback is None:
            self.screen_kind = styledlist.FALLBACK
            self.screen_reason = styled
            LOG.warning("the styled menu screen cannot be built here (%s); the image's own "
                        "list is used", styled)
        else:
            LOG.warning("no menu screen can be built on this image: %s; %s", styled, fallback)
            return False
        from .hotkey import Hotkey

        self._hotkey = Hotkey(self.hotkey_pressed)
        self._hotkey.bind(self.value("menu_hotkey"))
        LOG.info("the receiver menu is ready (screen %s, hotkey %s)", self.screen_kind,
                 self._hotkey.bound or "-")
        return True

    def claimed(self):
        return self.screen_kind is not None

    def stop(self):
        try:
            self._ticker.stop()
            if self._hotkey is not None:
                self._hotkey.unbind()
            for controller in list(self._controllers):
                controller._close()
        except Exception:
            LOG.exception("letting go of the receiver menu raised; the receiver is unaffected")

    def rebind_hotkey(self):
        """`menu_hotkey` changed through `cmd/config`: bind the new key, let go of the old."""
        if self._hotkey is not None:
            self._hotkey.bind(self.value("menu_hotkey"))

    # ---------------------------------------------------------------- `info` --

    def info(self):
        """`info.menu`: which screen this image builds, and which key opens it."""
        hotkey, reason = (None, None)
        if self._hotkey is not None:
            hotkey, reason = self._hotkey.report()
        return {"screen": self.screen_kind, "hotkey": hotkey, "reason": reason}

    # ---------------------------------------------------------------- topics --

    def menu_topic(self):
        node = self.bridge.node_id if self.bridge is not None else ""
        return MENU_TOPIC_PREFIX + node if node else None

    def status_topic(self):
        if self.bridge is None:
            return None
        return self.bridge.discovery_prefix + STATUS_SUFFIX

    def topics(self):
        return [topic for topic in (self.menu_topic(), self.status_topic()) if topic]

    def subscribe(self, client):
        for topic in self.topics():
            client.subscribe(topic, qos=SUBSCRIBE_QOS)

    def handles(self, topic):
        return topic in self.topics()

    def on_message(self, topic, payload, retain=False):
        if topic == self.menu_topic():
            self.on_menu(payload)
        elif topic == self.status_topic():
            self.on_status(payload)

    def on_menu(self, payload):
        try:
            menu = parse_menu(payload)
        except UnknownVersion as error:
            if error.version not in self._versions_logged:
                self._versions_logged.add(error.version)
                LOG.warning("ignoring the menu: %s", error)
            self.menu = None
            self.menu_seen = True
            self._menu_changed()
            return
        except InvalidMenu as error:
            LOG.warning("ignoring an invalid menu, keeping the one before it: %s", error)
            return
        self.menu = menu
        self.menu_seen = True
        if menu is None:
            LOG.info("Home Assistant has no menu for this receiver")
        else:
            LOG.debug("menu rev %s: %d item(s), %d row(s)", menu["rev"], len(menu["items"]),
                      len(menu["rows"]))
        self._menu_changed()

    def on_status(self, payload):
        if isinstance(payload, (bytes, bytearray)):
            try:
                payload = bytes(payload).decode("utf-8")
            except UnicodeDecodeError:
                return
        text = str(payload or "").strip().lower()
        if not text:
            status = None
        elif text in (ONLINE, OFFLINE):
            status = text
        else:
            LOG.debug("ignoring an unknown Home Assistant status")
            return
        if status != self.ha_status:
            LOG.info("Home Assistant says %s", status or "nothing")
            self.ha_status = status
            self.refresh_screens()

    def connection_changed(self):
        """The broker connection came or went: grey or ungrey, and drop what was not taken.

        A `menu_action` the broker has not acknowledged when the connection goes is taken out of
        the client's queue: sent after a reconnect it would be an OK from minutes ago (D64).
        """
        if self.bridge is not None and not self.bridge.connected:
            for info in self._unacknowledged:
                if not _acknowledged(info) and self.bridge.withdraw_event(info):
                    LOG.info("dropped a menu_action the broker had not acknowledged")
            self._unacknowledged = []
        self.refresh_screens()

    # ---------------------------------------------------------- reachability --

    def greyed(self):
        """Why the menu cannot be acted on now (D14), or None."""
        bridge = self.bridge
        if bridge is None or not bridge.connected:
            return GREY_BROKER
        if self.ha_status == OFFLINE:
            return GREY_HOME_ASSISTANT
        return None

    def actionable(self, row):
        return (
            not self.greyed()
            and row.get("type") in ACTIONS
            and bool(row.get("available"))
        )

    # --------------------------------------------------------------- pending --

    def pending_for(self, ident):
        return self.pending.get(ident)

    def _menu_changed(self):
        menu = self.menu
        for ident, mark in list(self.pending.items()):
            row = menu["rows"].get(ident) if menu is not None else None
            if row is None or row["type"] != mark["type"]:
                del self.pending[ident]
            elif row["value"] != mark["before"]:
                # Home Assistant changed it - to what was asked, or to what it could.
                del self.pending[ident]
        self._arm()
        self.refresh_screens()

    def _arm(self):
        self._ticker.stop()
        if not self.pending:
            return
        due = min(mark["deadline"] for mark in self.pending.values())
        wait = max(0, int(math.ceil((due - self.clock()) * 1000)))
        if not self._ticker.start(wait, True):
            LOG.debug("no timer for the pending mark; the next menu clears it")

    def _expire(self):
        now = self.clock()
        expired = [ident for ident, mark in self.pending.items() if mark["deadline"] <= now]
        for ident in expired:
            del self.pending[ident]
        if expired:
            LOG.info("Home Assistant changed nothing within %d s for %s", PENDING_SECONDS,
                     ", ".join(sorted(expired)))
            for controller in self._controllers:
                controller.notice = NOTICE_NOTHING_CHANGED
        self._arm()
        self.refresh_screens()

    # ---------------------------------------------------------------- acting --

    def send(self, row, action, value=None):
        """One `menu_action`. True when the client took it.

        Never for an indicator, an unavailable row, a greyed menu or a row the current menu
        does not hold with that type - whatever the screen thought it showed.
        """
        menu = self.menu
        if menu is None or self.greyed():
            return False
        current = menu["rows"].get(row.get("id"))
        if current is None or current["type"] != row.get("type"):
            return False
        if ACTIONS.get(current["type"]) != action or not current["available"]:
            return False
        payload = {
            "id": secrets.token_hex(_ACTION_ID_BYTES),
            "rev": menu["rev"],
            "row": current["id"],
            "action": action,
            "ts": int(time.time()),
        }
        if action == "set":
            if current["type"] == INPUT:
                value = str(value)[: current["max_length"]]
            payload["value"] = value
        info = self.bridge.publish_event(ACTION_SUFFIX, payload, qos=ACTION_QOS)
        if info is None or getattr(info, "rc", 0):
            # The socket was already gone: the client keeps a QoS 1 message for the next
            # connect, and an action must never arrive late (D64).
            if info is not None:
                self.bridge.withdraw_event(info)
            LOG.warning("menu_action for %s was not sent", current["id"])
            return False
        self._unacknowledged = [
            sent for sent in self._unacknowledged if not _acknowledged(sent)
        ] + [info]
        LOG.info("menu_action %s %s on %s (rev %s)", payload["id"], action, current["id"],
                 menu["rev"])
        if current["type"] in PENDING_TYPES:
            self.pending[current["id"]] = {
                "type": current["type"],
                "before": current["value"],
                "sent": value if action == "set" else None,
                "deadline": self.clock() + PENDING_SECONDS,
            }
            self._arm()
        return True

    # ---------------------------------------------------------------- screens --

    def refresh_screens(self):
        for controller in list(self._controllers):
            controller.refresh()

    def controller_closed(self, controller):
        if controller in self._controllers:
            self._controllers.remove(controller)

    def hotkey_pressed(self, session=None):
        """The hotkey: True when it opened the menu, so the key is consumed.

        Only over live TV or a player (D67, `hotkey.py`): the info bar, the image's movie
        player, or its media player is the executing dialog. Anywhere else the key goes on.
        """
        session = session or self.session
        if session is None or self._controllers:
            return False
        dialog = getattr(session, "current_dialog", None)
        if dialog is None or not hotkey_screen(dialog):
            return False
        return self.open(session)

    def open(self, session=None):
        """Open the menu on the television: the styled screen, or the image's own list."""
        session = session or self.session
        if session is None or self.screen_kind is None or self._controllers:
            return False
        from . import styledlist

        before = (self.screen_kind, self.screen_reason)
        controller = None
        if self.screen_kind == styledlist.STYLED:
            controller = MenuController(self, adjustable=True)
            refusal = styledlist.open_styled(session, controller)
            if refusal is not None:
                LOG.warning("the styled menu screen could not be built (%s); the image's own "
                            "list is used", refusal)
                self.screen_kind = styledlist.FALLBACK
                self.screen_reason = refusal
                controller = None
        if controller is None:
            controller = MenuController(self, adjustable=False)
            refusal = styledlist.open_fallback(session, controller)
            if refusal is not None:
                LOG.warning("the menu could not be opened: %s", refusal)
                return False
        self._controllers.append(controller)
        if (self.screen_kind, self.screen_reason) != before and self.bridge is not None:
            self.bridge.announce_capabilities()
        return True
