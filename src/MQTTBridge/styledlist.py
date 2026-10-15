"""A styled list screen with computed geometry, and the image's own list as its fallback.

Written for the receiver menu (`menu.py`) and meant to be reused: the screen knows nothing about
menus. A *controller* hands it a view - a title, a marker, rows, a notice and a legend - and gets
every key back with the id of the selected row (`MenuController` is the one in use). A row is a
dict: `id`, `icon` (a name, or None for none), `label`, `state` (text on the right), `colour`
(`green`, `red` or None), `dim` and `pending`.

**One code path, computed geometry (D20).** As the toast does (`toast.py`): `f = desktop height /
720`, every number formatted into the embedded skin before `Screen.__init__`, and the rows built in
Python with `eListboxPythonMultiContent`, their pixel positions computed the same way. No skin
template and no skin expression, which not every image has. The spike on OpenViX 6.6 read the
widgets back at exactly the computed numbers.

**Every optional drawing call has a second way.** A name being importable proves nothing - on
OpenViX 6.6 `MultiContentEntryRectangle` imports and raises when called - so nothing here draws a
rectangle, and the icon is tried with `flags=BT_SCALE | BT_KEEP_ASPECT_RATIO` (one 64-px PNG for
every desktop), then without `flags`, then not at all. `RT_ELLIPSIS` is added where the image has
it.

**The number keys** move the selection to the row of that number and do nothing else; OK acts
(D66). The image's `ChoiceBox` cannot be told that: a digit there either picks its row - and acts
- or does nothing. So the fallback is given no number keys at all.

**Decided once per opening (D21).** The styled screen is first built with
`session.instantiateDialog`, which constructs, skins and lays it out without touching the dialog
stack, and deleted again: only when that worked is it opened with `session.open`. A screen whose
constructor raises inside `session.open` would leave the session with the info bar's execution
ended and nothing executing - a television that no longer answers its remote. Anything that fails
in the trial is the reason the image's own `ChoiceBox` is used instead, as it is: the same rows
and ids, no icon and no state.

**The keys.** OK, EXIT, LEFT and RIGHT through `OkCancelActions` and `DirectionActions`, and 1-9
through `NumberActions`, at priority -1, before the list's own bindings. LEFT and RIGHT are
consumed only when the controller used them (a slider); otherwise they go on to the list, which
pages. UP and DOWN are the list's own. No colour key is bound.

Every entry point catches what it raises: a screen that fails is a line in the log.
"""

import importlib
import os
import re

from .enigma2 import enigma_attribute
from .i18n import _
from .log import get_logger

LOG = get_logger("styledlist")

STYLED = "styled"
FALLBACK = "fallback"

# What a skin calls the screen if it wants to restyle it.
SKIN_NAME = "MQTTBridgeList"

ICON_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
NEUTRAL_ICON = "neutral"
_ICON_NAME = re.compile(r"[a-z0-9-]{1,32}")

# The shape at a 720-line desktop; every number is multiplied by `height / 720`. A starting
# point measured on one skin (Magic-FHD), not a rule of any image.
BASE_LINES = 720
BASE = {
    "width": 640,
    "pad": 16,
    "header": 40,
    "marker_width": 220,
    "row": 46,
    "icon": 30,
    "gap": 6,
    "notice": 28,
    "legend": 24,
    "title_font": 26,
    "marker_font": 16,
    "row_font": 22,
    "state_font": 20,
    "notice_font": 18,
    "legend_font": 16,
}
VISIBLE_ROWS = 8
STATE_SHARE = 0.34

# enigma2 skin colours are #AARRGGBB, alpha 00 opaque; a list entry's colour is 0xRRGGBB.
BACKGROUND = "#08101820"
PANEL = "#0018242f"
SELECTED = "#001f5f8b"
TITLE_COLOUR = "#0041bdf5"
MARKER_COLOUR = "#007a8894"
NOTICE_COLOUR = "#00e6b422"
LEGEND_COLOUR = "#007a8894"
TEXT = 0xE8EEF2
TEXT_SELECTED = 0xFFFFFF
DIM = 0x7A8894
DIM_SELECTED = 0xB8C2CA
PENDING = 0xE6B422
STATE_COLOURS = {"green": 0x4CAF50, "red": 0xE5534B}
# On the selection bar a plain green or red is hard to read: lighter ones.
STATE_COLOURS_SELECTED = {"green": 0x9BE59F, "red": 0xFF9A94}

_screen_class = None


# ------------------------------------------------------------------ the geometry --


def geometry(desktop_width, desktop_height, rows=VISIBLE_ROWS):
    """Every number the skin and the rows need, as plain desktop pixels."""
    factor = float(desktop_height) / BASE_LINES
    shape = {name: int(value * factor) for name, value in BASE.items()}
    visible = max(1, min(int(rows), VISIBLE_ROWS))
    shape["width"] = max(1, min(shape["width"], int(desktop_width) - 2 * shape["pad"]))
    shape["inner"] = max(1, shape["width"] - 2 * shape["pad"])
    shape["list_height"] = VISIBLE_ROWS * shape["row"]
    shape["visible"] = visible
    shape["list_y"] = shape["pad"] + shape["header"]
    shape["notice_y"] = shape["list_y"] + shape["list_height"] + shape["gap"]
    shape["legend_y"] = shape["notice_y"] + shape["notice"]
    shape["height"] = shape["legend_y"] + shape["legend"] + shape["pad"]
    shape["x"] = max(0, (int(desktop_width) - shape["width"]) // 2)
    shape["y"] = max(0, (int(desktop_height) - shape["height"]) // 2)
    shape["title_width"] = max(1, shape["inner"] - shape["marker_width"])
    shape["marker_x"] = shape["pad"] + shape["title_width"]
    shape["state_width"] = int(shape["inner"] * STATE_SHARE)
    shape["factor"] = factor
    return shape


def build_skin(shape):
    """The embedded skin, with every number already a plain integer."""
    return (
        '<screen name="{name}" position="{x},{y}" size="{width},{height}" '
        'flags="wfNoBorder" backgroundColor="{background}">'
        '<widget name="title" position="{pad},{pad}" size="{title_width},{header}" '
        'font="Regular;{title_font}" valign="center" foregroundColor="{title_colour}" '
        'backgroundColor="{background}" transparent="1" />'
        '<widget name="marker" position="{marker_x},{pad}" size="{marker_width},{header}" '
        'font="Regular;{marker_font}" valign="center" halign="right" '
        'foregroundColor="{marker_colour}" backgroundColor="{background}" transparent="1" />'
        '<widget name="list" position="{pad},{list_y}" size="{inner},{list_height}" '
        'itemHeight="{row}" scrollbarMode="showOnDemand" backgroundColor="{panel}" '
        'backgroundColorSelected="{selected}" transparent="0" />'
        '<widget name="notice" position="{pad},{notice_y}" size="{inner},{notice}" '
        'font="Regular;{notice_font}" valign="center" foregroundColor="{notice_colour}" '
        'backgroundColor="{background}" transparent="1" />'
        '<widget name="legend" position="{pad},{legend_y}" size="{inner},{legend}" '
        'font="Regular;{legend_font}" valign="center" foregroundColor="{legend_colour}" '
        'backgroundColor="{background}" transparent="1" />'
        "</screen>"
    ).format(
        name=SKIN_NAME,
        background=BACKGROUND,
        panel=PANEL,
        selected=SELECTED,
        title_colour=TITLE_COLOUR,
        marker_colour=MARKER_COLOUR,
        notice_colour=NOTICE_COLOUR,
        legend_colour=LEGEND_COLOUR,
        **shape
    )


def desktop_geometry():
    get_desktop = enigma_attribute("getDesktop")
    if get_desktop is None:
        return None
    size = get_desktop(0).size()
    return geometry(size.width(), size.height())


# ------------------------------------------------------------------ what exists --


def _missing_names(module, names):
    """The first of `names` that `module` cannot give, as a sentence; None when it has all."""
    try:
        imported = importlib.import_module(module)
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    for name in names:
        if getattr(imported, name, None) is None:
            return f"{module} has no {name}"
    return None


# What the styled screen is built from.
STYLED_NAMES = (
    ("Components.ActionMap", ("ActionMap", "NumberActionMap")),
    ("Components.Label", ("Label",)),
    ("Components.MenuList", ("MenuList",)),
    ("Components.MultiContent", ("MultiContentEntryText",)),
    ("Screens.Screen", ("Screen",)),
    ("Tools.LoadPixmap", ("LoadPixmap",)),
)


def styled_refusal():
    """Why the styled screen cannot be built on this image, or None. Imports only."""
    for module, names in STYLED_NAMES:
        refusal = _missing_names(module, names)
        if refusal is not None:
            return refusal
    for name in ("eListboxPythonMultiContent", "gFont", "RT_HALIGN_LEFT", "RT_HALIGN_RIGHT",
                 "RT_VALIGN_CENTER", "getDesktop"):
        if enigma_attribute(name) is None:
            return "enigma has no " + name
    return None


def fallback_refusal():
    """Why the image's own list cannot be used, or None."""
    return _missing_names("Screens.ChoiceBox", ("ChoiceBox",))


# -------------------------------------------------------------------- the rows --


def icon_path(name):
    """The PNG for an icon name; the neutral icon for a name with no file.

    A name is a file name in `icons/` and nothing else: anything that is not lower-case letters,
    digits and hyphens - a path, above all - is the neutral icon.
    """
    if isinstance(name, str) and _ICON_NAME.fullmatch(name):
        path = os.path.join(ICON_DIRECTORY, name + ".png")
        if os.path.isfile(path):
            return path
    return os.path.join(ICON_DIRECTORY, NEUTRAL_ICON + ".png")


def _pixmap_entry(multicontent, flags, position, size, png):
    """The icon, scaled where the image can; the call without flags where it cannot; or None."""
    factory = getattr(multicontent, "MultiContentEntryPixmapAlphaBlend", None)
    if factory is None or png is None:
        return None
    attempts = ({"flags": flags}, {}) if flags else ({},)
    for extra in attempts:
        try:
            return factory(pos=position, size=size, png=png, **extra)
        except TypeError:
            continue
        except Exception:
            LOG.debug("the icon could not be placed in the row")
            return None
    return None


def build_row(row, shape, load, names):
    """One list entry: `[id, icon?, label, state?]`, in the image's multi-content form.

    `load(icon name)` gives the pixmap; `names` holds what `enigma` and `Components.MultiContent`
    offer, looked up once per screen.
    """
    multicontent = names["multicontent"]
    text_entry = multicontent.MultiContentEntryText
    height = shape["row"]
    pad = shape["pad"]
    x = pad // 2
    entry = [row["id"]]
    if row.get("icon"):
        size = shape["icon"]
        made = _pixmap_entry(multicontent, names["scale_flags"], (x, (height - size) // 2),
                             (size, size), load(row["icon"]))
        if made is not None:
            entry.append(made)
        x += size + pad // 2
    state = row.get("state") or ""
    state_width = shape["state_width"] if state else 0
    label_width = max(1, shape["inner"] - x - state_width - pad // 2)
    dim = bool(row.get("dim"))
    entry.append(text_entry(
        pos=(x, 0), size=(label_width, height), font=0,
        flags=names["left"] | names["vcenter"] | names["ellipsis"],
        text=row.get("label") or "",
        color=DIM if dim else TEXT, color_sel=DIM_SELECTED if dim else TEXT_SELECTED,
    ))
    if state:
        if dim:
            colour, selected = DIM, DIM_SELECTED
        elif row.get("pending"):
            colour = selected = PENDING
        else:
            colour = STATE_COLOURS.get(row.get("colour"), TEXT)
            selected = STATE_COLOURS_SELECTED.get(row.get("colour"), TEXT_SELECTED)
        entry.append(text_entry(
            pos=(shape["inner"] - state_width - pad // 2, 0), size=(state_width, height),
            font=1, flags=names["right"] | names["vcenter"] | names["ellipsis"],
            text=state, color=colour, color_sel=selected,
        ))
    return entry


def _names():
    import Components.MultiContent as multicontent

    def flag(name):
        value = enigma_attribute(name)
        return value if isinstance(value, int) else 0

    return {
        "multicontent": multicontent,
        "left": flag("RT_HALIGN_LEFT"),
        "right": flag("RT_HALIGN_RIGHT"),
        "vcenter": flag("RT_VALIGN_CENTER"),
        # Not on every image; where it is missing the row's width cuts the text instead.
        "ellipsis": _optional_flag("RT_ELLIPSIS"),
        "scale_flags": _optional_flag("BT_SCALE") | _optional_flag("BT_KEEP_ASPECT_RATIO"),
    }


def _optional_flag(name):
    try:
        import enigma

        value = getattr(enigma, name, 0)
    except Exception:
        return 0
    return value if isinstance(value, int) else 0


# ------------------------------------------------------------------ the screen --


def screen_class():
    """The styled screen's class, built on first use so importing this needs nothing."""
    global _screen_class
    if _screen_class is not None:
        return _screen_class

    from Components.ActionMap import ActionMap, NumberActionMap
    from Components.Label import Label
    from Components.MenuList import MenuList
    from enigma import eListboxPythonMultiContent, gFont
    from Screens.Screen import Screen
    from Tools.LoadPixmap import LoadPixmap

    class MQTTBridgeList(Screen):
        """Title, marker, the rows, a notice and the keys' legend. Nothing else."""

        def __init__(self, session, shape, controller):
            self.skin = build_skin(shape)
            Screen.__init__(self, session)
            self.shape = shape
            self.controller = controller
            self._names = _names()
            self._pixmaps = {}
            self._level = None
            self._ids = []
            self["title"] = Label("")
            self["marker"] = Label("")
            self["notice"] = Label("")
            self["legend"] = Label("")
            self["list"] = MenuList([], True, content=eListboxPythonMultiContent)
            content = self["list"].l
            content.setFont(0, gFont("Regular", shape["row_font"]))
            content.setFont(1, gFont("Regular", shape["state_font"]))
            content.setItemHeight(shape["row"])
            self["menuActions"] = ActionMap(
                ["OkCancelActions", "DirectionActions"],
                {"ok": self.key_ok, "cancel": self.key_cancel,
                 "left": self.key_left, "right": self.key_right},
                -1,
            )
            self["menuNumbers"] = NumberActionMap(
                ["NumberActions"], {str(number): self.key_number for number in range(1, 10)}, -1
            )
            # Built here, so that the trial build in `open_styled` draws every row once.
            self.render(controller.view())
            # The list has no widget until the skin is applied, and a move before that is
            # dropped by every image's `MenuList`: the first selection is made again then.
            self.onLayoutFinish.append(self._laid_out)
            self.onClose.append(self._closed)

        def _laid_out(self):
            self._level = None
            try:
                self.render(self.controller.view())
            except Exception:
                LOG.exception("the menu could not be drawn after its layout")

        # ------------------------------------------------------------ drawing --

        def _pixmap(self, name):
            path = icon_path(name)
            if path not in self._pixmaps:
                try:
                    self._pixmaps[path] = LoadPixmap(path)
                except Exception:
                    LOG.debug("the icon %s could not be loaded", os.path.basename(path))
                    self._pixmaps[path] = None
            return self._pixmaps[path]

        def render(self, view):
            current = self.selected()
            index = self._index()
            rows = view.get("rows") or []
            self["title"].setText(view.get("title") or "")
            self["marker"].setText(view.get("marker") or "")
            self["notice"].setText(view.get("notice") or "")
            self["legend"].setText(view.get("legend") or "")
            entries = [build_row(row, self.shape, self._pixmap, self._names) for row in rows]
            self._ids = [row["id"] for row in rows]
            self["list"].setList(entries)
            level = view.get("level")
            if level != self._level:
                self._level = level
                wanted = view.get("initial")
                self._move(self._ids.index(wanted) if wanted in self._ids else 0)
            elif current in self._ids:
                self._move(self._ids.index(current))
            elif self._ids:
                # The row has gone: the nearest one, where it was.
                self._move(min(index, len(self._ids) - 1))

        def _index(self):
            menu_list = self["list"]
            for name in ("getSelectedIndex", "getSelectionIndex"):
                getter = getattr(menu_list, name, None)
                if getter is not None:
                    try:
                        return int(getter() or 0)
                    except Exception:
                        continue
            return 0

        def _move(self, index):
            if self._ids:
                self["list"].moveToIndex(max(0, min(int(index), len(self._ids) - 1)))

        def selected(self):
            try:
                entry = self["list"].getCurrent()
            except Exception:
                return None
            return entry[0] if entry else None

        def select(self, ident):
            if ident in self._ids:
                self._move(self._ids.index(ident))

        # ------------------------------------------------------------- keys --

        def key_ok(self):
            try:
                self.controller.key_ok(self.selected())
            except Exception:
                LOG.exception("OK on the menu raised")

        def key_cancel(self):
            try:
                self.controller.key_cancel(self.selected())
            except Exception:
                LOG.exception("EXIT on the menu raised; closing it")
                self.close_menu()

        def _adjust(self, direction):
            try:
                if self.controller.key_adjust(self.selected(), direction):
                    return 1
            except Exception:
                LOG.exception("LEFT or RIGHT on the menu raised")
            # Not consumed: the list pages.
            return 0

        def key_left(self):
            return self._adjust(-1)

        def key_right(self):
            return self._adjust(1)

        def key_number(self, number):
            try:
                self.controller.key_number(int(number))
            except Exception:
                LOG.exception("a number key on the menu raised")

        # ---------------------------------------------------- the controller --

        def open_keyboard(self, title, text, callback):
            try:
                from Screens.VirtualKeyBoard import VirtualKeyBoard

                self.session.openWithCallback(callback, VirtualKeyBoard, title=title, text=text)
            except Exception:
                LOG.exception("the image's keyboard could not be opened")
                callback(None)

        def close_menu(self):
            try:
                self.close()
            except Exception:
                LOG.exception("the menu screen could not be closed")

        def _closed(self):
            controller = self.controller
            if controller is not None and not controller.closed:
                controller.closed = True
                controller.publisher.controller_closed(controller)

    _screen_class = MQTTBridgeList
    return _screen_class


def open_styled(session, controller):
    """Open the styled screen for `controller`. None when it is open, otherwise why not.

    Built once on trial with `instantiateDialog` - off the dialog stack - and deleted, then
    opened for real. See the module for why a constructor must never fail inside `open`.
    """
    try:
        shape = desktop_geometry()
        if shape is None:
            return "the image will not say how big its desktop is"
        cls = screen_class()
        trial = session.instantiateDialog(cls, shape, _TrialController(controller))
        if trial is None:
            return "the image returned no screen"
        session.deleteDialog(trial)
    except Exception as error:
        LOG.exception("the styled menu screen could not be built")
        return f"{type(error).__name__}: {error}"
    try:
        screen = session.open(cls, shape, controller)
    except Exception as error:
        LOG.exception("the styled menu screen could not be opened")
        return f"{type(error).__name__}: {error}"
    controller.attach(screen)
    return None


class _TrialController:
    """The trial build's controller: the same view, and no key or close reaches the real one."""

    def __init__(self, controller):
        self._controller = controller
        self.closed = True
        self.publisher = controller.publisher

    def view(self):
        return self._controller.view()

    def __getattr__(self, name):
        if name.startswith("key_"):
            return lambda *arguments: None
        raise AttributeError(name)


# --------------------------------------------------------------- the fallback --


class ChoiceBoxAdapter:
    """The image's own `ChoiceBox`, as it is, in the role of the styled screen (D21).

    The same rows and ids; no icon, no state, no colour. No number keys: the image's list acts
    on a digit - `goKey` runs its entry - and has no way to only move the selection, which is
    all a digit does in the menu (D66). An empty key is the one every image reads as "none"
    (OpenViX and OpenATV skip it, OpenPLi maps it to the key named "", which no button sends). A pick is the controller's OK and EXIT its EXIT, and the list is opened again after
    each until the controller closes - so the menu stays on screen as the styled one does. The
    title carries the notice, which is the one line the image's list gives to say anything. A
    menu that changes while the list is open shows at the next pick.
    """

    def __init__(self, session, controller):
        self.session = session
        self.controller = controller
        self._open = False
        self._closing = False

    def render(self, view):
        """Asked by the controller; the list cannot change while it is open."""
        self._view = view

    def select(self, ident):
        pass

    def open(self):
        from Screens.ChoiceBox import ChoiceBox

        view = self.controller.view()
        rows = view.get("rows") or []
        title = view.get("title") or ""
        if view.get("notice"):
            title = title + "\n" + view["notice"]
        choices = [(row.get("label") or "", row["id"]) for row in rows]
        if not choices:
            # The image's list needs something to show: the notice says why there is nothing.
            choices = [(view.get("notice") or _("There is no menu from Home Assistant yet."),
                        None)]
        keys = [""] * len(choices)
        initial = view.get("initial")
        ids = [choice[1] for choice in choices]
        selection = ids.index(initial) if initial in ids else 0
        self._open = True
        self.session.openWithCallback(self._picked, ChoiceBox, title=title, list=choices,
                                      keys=keys, selection=selection)

    def _picked(self, choice):
        self._open = False
        try:
            if self._closing or self.controller.closed:
                return
            if choice is None:
                self.controller.key_cancel(None)
            elif choice[1] is not None:
                self.controller.key_ok(choice[1])
            if not self.controller.closed and not self._waiting:
                self.open()
        except Exception:
            LOG.exception("the image's list could not go on with the menu")
            self.close_menu()

    _waiting = False

    def open_keyboard(self, title, text, callback):
        from Screens.VirtualKeyBoard import VirtualKeyBoard

        def typed(answer):
            self._waiting = False
            callback(answer)
            if not self.controller.closed:
                self.open()

        self._waiting = True
        self.session.openWithCallback(typed, VirtualKeyBoard, title=title, text=text)

    def close_menu(self):
        self._closing = True
        controller = self.controller
        if not controller.closed:
            controller.closed = True
            controller.publisher.controller_closed(controller)


def open_fallback(session, controller):
    """Open the image's own list for `controller`. None when it is open, otherwise why not."""
    adapter = ChoiceBoxAdapter(session, controller)
    controller.attach(adapter)
    try:
        adapter.open()
    except Exception as error:
        LOG.exception("the image's list could not be opened for the menu")
        controller.attach(None)
        return f"{type(error).__name__}: {error}"
    return None
