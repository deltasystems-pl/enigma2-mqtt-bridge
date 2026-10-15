"""The styled list screen: computed geometry, the rows it draws, and the second way of each call.

The numbers are the spike's (OpenViX 6.6, Magic-FHD, 1920x1080): the screen reads its widgets
back at exactly what was computed, so here the computation itself is held, and the skin is laid
out through the session model's `readSkin`/`applySkin`, which refuse anything but plain integers.
"""

import pytest
from conftest import DESKTOP, Session, eSize

from MQTTBridge import styledlist
from MQTTBridge.menu import ICON_NAMES


class Controller:
    """The least a screen needs: a view, and somewhere for the keys to go."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [
            {"id": "a", "icon": "lamp", "label": "Lamp", "state": "on", "colour": "green",
             "dim": False, "pending": False},
            {"id": "b", "icon": "rocket", "label": "Unknown icon", "state": "", "colour": None,
             "dim": True, "pending": False},
            {"id": "c", "icon": None, "label": "No icon", "state": "\u2026", "colour": None,
             "dim": False, "pending": True},
        ]
        self.closed = False
        self.publisher = None
        self.keys = []

    def view(self):
        return {"title": "Home Assistant", "marker": "from Home Assistant", "rows": self.rows,
                "notice": "", "legend": "OK select   EXIT back", "greyed": False,
                "level": "top", "initial": None}

    def key_ok(self, selected):
        self.keys.append(("ok", selected))

    def key_cancel(self, selected):
        self.keys.append(("cancel", selected))

    def key_adjust(self, selected, direction):
        self.keys.append(("adjust", selected, direction))
        return selected == "a"

    def key_number(self, number):
        self.keys.append(("number", number))


def test_the_geometry_scales_with_the_desktop_height():
    small = styledlist.geometry(1280, 720)
    full = styledlist.geometry(1920, 1080)
    assert small["row"] == 46 and full["row"] == 69
    assert small["width"] == 640 and full["width"] == 960
    assert small["icon"] == 30 and full["icon"] == 45
    assert full["list_height"] == 8 * 69
    assert full["x"] == (1920 - 960) // 2
    assert full["y"] == (1080 - full["height"]) // 2
    assert full["height"] == full["legend_y"] + full["legend"] + full["pad"]


def test_a_narrow_desktop_never_gets_a_wider_screen():
    shape = styledlist.geometry(500, 720)
    assert shape["width"] == 500 - 2 * shape["pad"]
    assert shape["x"] >= 0


def test_the_skin_is_plain_integers_and_lays_out_as_computed():
    session = Session(nav=None)
    shape = styledlist.desktop_geometry()
    screen = session.instantiateDialog(styledlist.screen_class(), shape, Controller())
    assert screen.instance.size() == eSize(shape["width"], shape["height"])
    assert screen["list"].instance.size() == eSize(shape["inner"], shape["list_height"])
    assert screen["list"].skinAttributes and ("itemHeight", str(shape["row"])) in \
        screen["list"].skinAttributes
    assert screen.skinName == "MQTTBridgeList"


def test_a_720_line_desktop_gets_the_720_numbers():
    DESKTOP.resize(eSize(1280, 720))
    shape = styledlist.desktop_geometry()
    assert shape["row"] == 46
    assert shape["factor"] == 1.0


def _rows(controller=None):
    controller = controller or Controller()
    session = Session(nav=None)
    screen = session.instantiateDialog(styledlist.screen_class(),
                                       styledlist.desktop_geometry(), controller)
    return screen


def test_a_row_draws_its_icon_label_and_coloured_state():
    screen = _rows()
    entry = screen["list"].list[0]
    assert entry[0] == "a"
    icon, label, state = entry[1:]
    assert icon["kind"] == "pixmap"
    assert icon["png"].path.endswith("icons/lamp.png")
    assert icon["flags"] == 128 | 256, "scaled, keeping the aspect ratio"
    assert label["text"] == "Lamp" and label["color"] == styledlist.TEXT
    assert state["text"] == "on"
    assert state["color"] == styledlist.STATE_COLOURS["green"]
    assert state["color_sel"] == styledlist.STATE_COLOURS_SELECTED["green"]
    assert label["flags"] & 64, "cut with an ellipsis where the image can"


def test_an_unknown_icon_is_drawn_neutral_and_a_dim_row_dims_its_text():
    screen = _rows()
    entry = screen["list"].list[1]
    assert entry[1]["png"].path.endswith("icons/neutral.png")
    assert entry[2]["color"] == styledlist.DIM


def test_a_row_without_an_icon_has_none_and_a_pending_state_has_its_colour():
    screen = _rows()
    entry = screen["list"].list[2]
    assert [part["kind"] for part in entry[1:]] == ["text", "text"]
    assert entry[2]["color"] == styledlist.PENDING


def test_an_image_that_refuses_flags_gets_the_icon_without_them(monkeypatch):
    import Components.MultiContent as multicontent

    calls = []

    def without_flags(pos=(0, 0), size=(0, 0), png=None, backcolor=None, backcolor_sel=None):
        calls.append(pos)
        return {"kind": "pixmap", "png": png, "flags": None}

    monkeypatch.setattr(multicontent, "MultiContentEntryPixmapAlphaBlend", without_flags)
    screen = _rows()
    # Two rows with an icon; the call with flags failed in Python before the image's code ran.
    assert screen["list"].list[0][1]["flags"] is None
    assert screen["list"].list[1][1]["flags"] is None
    assert calls and all(position == (12, 12) for position in calls)


def test_an_image_whose_pixmap_call_raises_draws_the_row_without_an_icon(monkeypatch):
    import Components.MultiContent as multicontent

    def broken(*args, **kwargs):
        raise RuntimeError("no TYPE_PIXMAP_ALPHABLEND")

    monkeypatch.setattr(multicontent, "MultiContentEntryPixmapAlphaBlend", broken)
    screen = _rows()
    assert [part["kind"] for part in screen["list"].list[0][1:]] == ["text", "text"]


def test_an_image_without_the_pixmap_call_draws_the_row_without_an_icon(monkeypatch):
    import Components.MultiContent as multicontent

    monkeypatch.delattr(multicontent, "MultiContentEntryPixmapAlphaBlend")
    screen = _rows()
    assert [part["kind"] for part in screen["list"].list[0][1:]] == ["text", "text"]


def test_no_row_ever_asks_for_a_rectangle(monkeypatch):
    """Importable and raising on OpenViX 6.6: the screen never calls it."""
    import Components.MultiContent as multicontent

    called = []
    monkeypatch.setattr(multicontent, "MultiContentEntryRectangle",
                        lambda *a, **k: called.append(1))
    _rows()
    assert called == []


def test_left_and_right_are_consumed_only_when_the_controller_used_them():
    controller = Controller()
    screen = _rows(controller)
    screen.select("a")
    assert screen.key_right() == 1
    screen.select("b")
    assert screen.key_left() == 0
    assert ("adjust", "b", -1) in controller.keys


def test_the_screen_binds_ok_exit_arrows_and_numbers_but_no_colour_key():
    screen = _rows()
    actions = screen["menuActions"]
    assert set(actions.contexts) == {"OkCancelActions", "DirectionActions"}
    assert set(actions.actions) == {"ok", "cancel", "left", "right"}
    assert actions.prio == -1
    numbers = screen["menuNumbers"]
    assert numbers.contexts == ["NumberActions"]
    assert set(numbers.actions) == {str(number) for number in range(1, 10)}


def test_a_key_that_raises_never_reaches_the_main_loop():
    class Raising(Controller):
        def key_ok(self, selected):
            raise RuntimeError("boom")

        def key_adjust(self, selected, direction):
            raise RuntimeError("boom")

    screen = _rows(Raising())
    screen.key_ok()
    assert screen.key_left() == 0


@pytest.mark.parametrize("name", ICON_NAMES)
def test_every_icon_name_has_its_file(name):
    path = styledlist.icon_path(name)
    assert path.endswith("/" + name + ".png")


def test_a_name_with_no_file_is_the_neutral_icon():
    assert styledlist.icon_path("rocket").endswith("/neutral.png")
    assert styledlist.icon_path(None).endswith("/neutral.png")
    assert styledlist.icon_path("../plugin").endswith("/neutral.png")


def test_the_trial_build_refusal_is_a_sentence_not_an_exception(monkeypatch):
    class Broken:
        def __init__(self, *args, **kwargs):
            raise KeyError("list")

    monkeypatch.setattr(styledlist, "screen_class", lambda: Broken)

    class Recording:
        opened = []

        def instantiateDialog(self, *args):
            return args[0](self, *args[1:])

        def open(self, *args):
            self.opened.append(args)

    session = Recording()
    refusal = styledlist.open_styled(session, Controller())
    assert refusal == "KeyError: 'list'"
    assert session.opened == []
