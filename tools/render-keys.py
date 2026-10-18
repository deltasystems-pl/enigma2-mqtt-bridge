#!/usr/bin/env python3
"""Draw the keys a list of choices shows on its rows: four colour buttons and a box for a digit.

A row of a list of choices (`src/MQTTBridge/choicelist.py`) shows its shortcut on the right: a
filled rounded button in the key's colour, or the digit in a small box. Each is one 64x64 RGBA
PNG in `src/MQTTBridge/keys/`, scaled by the image like the menu's icons; the digit itself is
text, drawn over the box.

The shapes are this project's own, written below as SVG path data, and rasterised by the
standard-library rasteriser of `tools/render-icons.py`, so the same shapes give the same bytes on
any machine:

    tools/render-keys.py           write the PNGs
    tools/render-keys.py --check   compare them with the files instead; a test does the same

The colours are `choicelist.KEY_RGB`, which the second way - a filled rectangle where the image
cannot place a pixmap - uses too; a test holds the two together.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
KEY_DIRECTORY = REPO_ROOT / "src" / "MQTTBridge" / "keys"
SIZE = 64

# A button 60 wide and 40 high in the middle of the square - `choicelist.BUTTON_SHARE` of it -
# with corners of radius 10.
BUTTON = ("M12 12 H52 A10 10 0 0 1 62 22 V42 A10 10 0 0 1 52 52 H12 "
          "A10 10 0 0 1 2 42 V22 A10 10 0 0 1 12 12 Z")
# A square outline four units wide, the inside drawn the other way round so it stays empty.
BOX = ("M16 6 H48 A10 10 0 0 1 58 16 V48 A10 10 0 0 1 48 58 H16 A10 10 0 0 1 6 48 "
       "V16 A10 10 0 0 1 16 6 Z "
       "M16 10 A6 6 0 0 0 10 16 V48 A6 6 0 0 0 16 54 H48 A6 6 0 0 0 54 48 V16 "
       "A6 6 0 0 0 48 10 Z")

KEYS = {
    "red": (BUTTON, (0xE5, 0x53, 0x4B)),
    "green": (BUTTON, (0x4C, 0xAF, 0x50)),
    "yellow": (BUTTON, (0xE8, 0xC5, 0x47)),
    "blue": (BUTTON, (0x4C, 0x8F, 0xE0)),
    # The menu's dimmed text on the selection bar: light enough on the panel and on the bar.
    "box": (BOX, (0xB8, 0xC2, 0xCA)),
}


def _icons():
    spec = importlib.util.spec_from_file_location("render_icons",
                                                  REPO_ROOT / "tools" / "render-icons.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render(name: str) -> bytes:
    icons = _icons()
    path, colour = KEYS[name]
    rows = icons.coverage(icons.path_polygons(path), (SIZE, SIZE), SIZE)
    return icons.png_rgba([[int(round(value * 255)) for value in row] for row in rows], colour)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="compare with the files instead of writing them")
    args = parser.parse_args(argv)
    problems = 0
    KEY_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for name in KEYS:
        data = render(name)
        target = KEY_DIRECTORY / (name + ".png")
        if args.check:
            if not target.is_file() or target.read_bytes() != data:
                print(f"{target} is not what this tool draws")
                problems += 1
        else:
            target.write_bytes(data)
            print(f"wrote {target} ({len(data)} bytes)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
