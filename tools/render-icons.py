#!/usr/bin/env python3
"""Import the receiver menu's icons from Material Design Icons, and render them to PNG.

The receiver menu draws one 64x64 RGBA PNG for each icon name the topic contract lists
(docs/TOPICS.md, "The icon names"). The names are this project's; each one is drawn with one
icon of Material Design Icons (Pictogrammers, the set Home Assistant uses). Which one is in
`assets/icons/manifest.json`, together with where the SVG came from and what its upstream
metadata says about it.

Two steps, each reproducible:

    tools/render-icons.py import <path to svg-7.4.47.tgz>
    tools/render-icons.py render

`import` reads the npm package `@mdi/svg` - downloaded beforehand, this tool makes no network
request:

    curl -sO https://registry.npmjs.org/@mdi/svg/-/svg-7.4.47.tgz

It checks the archive against the SHA-512 the manifest names (the npm registry's `integrity`),
checks the licence of every icon it takes (below), copies each SVG to `assets/icons/mdi/`,
copies the upstream licence beside them, and writes each icon's metadata into the manifest.

`render` draws every SVG in `assets/icons/mdi/` into `src/MQTTBridge/icons/<name>.png`, and writes
the attribution that ships beside them, `src/MQTTBridge/icons/NOTICE`. It is a
rasteriser of its own, in the standard library, so that the same SVG gives the same bytes on any
machine and the build needs nothing installed: the path data is flattened into line segments,
filled with the non-zero rule SVG uses by default, sampled at 16 sub-scanlines a pixel row with
the exact horizontal coverage of each span, and written as an 8-bit RGBA PNG without any
time-dependent chunk. `--check` renders into memory and compares with the files instead of
writing them; a test does the same.

**The licence rule** (D57 in the project's plan): only icons under Apache-2.0, and no brand icon.
The package declares `"license": "Apache-2.0"`, and its LICENSE file says that the icons are
under Apache 2.0 - those Pictogrammers redistribute from Google's Material Icons and those they
distribute themselves - except icons "redistributed under their respective licenses", which in
this collection are the brand and logo icons. The metadata (`meta.json`) carries no licence
field per icon; what it does carry is each icon's `author`, its `tags` and whether it is
`deprecated`. So an icon is taken only when it is in `meta.json`, is not deprecated, and is not
tagged `Brand / Logo`; its author is recorded, `Google` meaning one of Google's Material Icons.
An icon that fails is refused, by name, and nothing is written.

**White glyphs.** Every PNG is a white glyph with the shape in its alpha channel. The menu draws
its own dark panel (`styledlist.py`) whatever the skin, so the glyph is always on a known
background; white keeps its contrast both on that panel and on the coloured bar of the selected
row, where a grey would vanish. The fallback screen, the image's own list, draws no icons.

Standard library only, Python 3.9, because it runs in the same test matrix as the plugin.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import re
import struct
import sys
import tarfile
import xml.etree.ElementTree as ElementTree
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS = REPO_ROOT / "assets" / "icons"
MANIFEST = ASSETS / "manifest.json"
SVG_DIRECTORY = ASSETS / "mdi"
PNG_DIRECTORY = REPO_ROOT / "src" / "MQTTBridge" / "icons"

BRAND_TAG = "Brand / Logo"
SUBSCANLINES = 16
CURVE_SEGMENTS = 24

NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
COMMAND = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]")


class IconError(ValueError):
    """An icon or the archive is not what the manifest says. Nothing is written."""


# ------------------------------------------------------------------ the manifest --


def read_manifest(path: Path = MANIFEST) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(manifest: dict, path: Path = MANIFEST) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------- import --


def _member(archive: tarfile.TarFile, name: str) -> bytes:
    member = archive.getmember("package/" + name)
    handle = archive.extractfile(member)
    if handle is None:
        raise IconError(f"{name} is not a file in the archive")
    return handle.read()


def licence_problems(name: str, meta: dict | None) -> list[str]:
    """Why the upstream icon `name` may not be used here; [] when it may."""
    if meta is None:
        return [f"{name}: not in the package's meta.json"]
    problems = []
    if meta.get("deprecated"):
        problems.append(f"{name}: deprecated upstream")
    if BRAND_TAG in (meta.get("tags") or []):
        problems.append(f"{name}: a brand icon, under its owner's terms rather than Apache-2.0")
    return problems


def import_archive(archive_path: Path, manifest_path: Path = MANIFEST) -> dict:
    manifest = read_manifest(manifest_path)
    source = manifest["source"]
    data = archive_path.read_bytes()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if integrity != source["integrity"]:
        raise IconError(f"{archive_path} is not {source['package']} {source['version']}: "
                        f"its integrity is {integrity}")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        package = json.loads(_member(archive, "package.json"))
        if package.get("name") != source["package"] or package.get("version") != source["version"]:
            raise IconError("the archive's package.json names another package or version")
        if package.get("license") != "Apache-2.0":
            raise IconError(f"the package declares the licence {package.get('license')!r}")
        # Upstream writes it with CRLF; the repository keeps every text file LF.
        licence = _member(archive, "LICENSE").decode("utf-8").replace("\r\n", "\n")
        if "Icons: Apache 2.0" not in licence:
            raise IconError("the package's LICENSE no longer puts its icons under Apache 2.0")
        meta = {entry["name"]: entry for entry in json.loads(_member(archive, "meta.json"))}
        problems = []
        for _name, entry in sorted(manifest["icons"].items()):
            problems += licence_problems(entry["mdi"], meta.get(entry["mdi"]))
        if problems:
            raise IconError("refused: " + "; ".join(problems))
        SVG_DIRECTORY.mkdir(parents=True, exist_ok=True)
        (SVG_DIRECTORY / "LICENSE").write_text(licence, encoding="utf-8")
        for _name, entry in sorted(manifest["icons"].items()):
            upstream = meta[entry["mdi"]]
            svg = _member(archive, "svg/" + entry["mdi"] + ".svg")
            (SVG_DIRECTORY / (entry["mdi"] + ".svg")).write_bytes(svg)
            entry["author"] = upstream.get("author")
            entry["added_in"] = upstream.get("version")
            entry["tags"] = list(upstream.get("tags") or [])
            entry["licence"] = "Apache-2.0"
    write_manifest(manifest, manifest_path)
    return manifest


# ------------------------------------------------------------------ path data --


def _tokens(data: str):
    """The path data as commands (str) and numbers (float), in order."""
    position = 0
    found = []
    while position < len(data):
        character = data[position]
        if character in " \t\r\n,":
            position += 1
            continue
        if COMMAND.match(character):
            found.append(character)
            position += 1
            continue
        match = NUMBER.match(data, position)
        if not match:
            raise IconError(f"unreadable path data at {data[position:position + 12]!r}")
        found.append(float(match.group(0)))
        position = match.end()
    return found


def _flag(tokens, index):
    """An arc flag: a 0 or a 1, which SVG allows to be packed against the next number."""
    value = tokens[index]
    if value not in (0.0, 1.0):
        raise IconError(f"an arc flag that is neither 0 nor 1: {value}")
    return bool(value)


def _cubic(p0, p1, p2, p3):
    points = []
    for step in range(1, CURVE_SEGMENTS + 1):
        t = step / CURVE_SEGMENTS
        u = 1 - t
        points.append((
            u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
        ))
    return points


def _quadratic(p0, p1, p2):
    points = []
    for step in range(1, CURVE_SEGMENTS + 1):
        t = step / CURVE_SEGMENTS
        u = 1 - t
        points.append((
            u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
            u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1],
        ))
    return points


def _arc(start, rx, ry, rotation, large, sweep, end):
    """SVG's endpoint arc as points, through the centre parameterisation (SVG 1.1, F.6.5)."""
    x1, y1 = start
    x2, y2 = end
    if (x1, y1) == (x2, y2):
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        return [end]
    phi = math.radians(rotation % 360)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy
    scale = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if scale > 1:
        root = math.sqrt(scale)
        rx, ry = rx * root, ry * root
    numerator = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    denominator = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    factor = math.sqrt(max(0.0, numerator / denominator)) if denominator else 0.0
    if large == sweep:
        factor = -factor
    cxp = factor * rx * y1p / ry
    cyp = -factor * ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2

    def angle(ux, uy, vx, vy):
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    theta = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    delta = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    segments = max(2, int(math.ceil(abs(delta) / (math.pi / 16))))
    points = []
    for step in range(1, segments + 1):
        t = theta + delta * step / segments
        x = cx + rx * math.cos(t) * cos_phi - ry * math.sin(t) * sin_phi
        y = cy + rx * math.cos(t) * sin_phi + ry * math.sin(t) * cos_phi
        points.append((x, y))
    points[-1] = end
    return points


def path_polygons(data: str) -> list[list[tuple[float, float]]]:
    """Closed polygons for one SVG path, in the path's own units."""
    tokens = _tokens(data)
    polygons = []
    current = []
    position = (0.0, 0.0)
    start = (0.0, 0.0)
    previous_control = None
    previous_command = ""
    index = 0
    command = None

    def number():
        nonlocal index
        value = tokens[index]
        if isinstance(value, str):
            raise IconError(f"a number was expected, found the command {value!r}")
        index += 1
        return value

    while index < len(tokens):
        if isinstance(tokens[index], str):
            command = tokens[index]
            index += 1
        elif command is None:
            raise IconError("path data that does not start with a command")
        elif command in "Mm":
            # Numbers after a moveto are implicit linetos.
            command = "L" if command == "M" else "l"
        relative = command.islower()
        kind = command.upper()
        ox, oy = position if relative else (0.0, 0.0)
        if kind == "Z":
            if current:
                polygons.append(current)
            current = []
            position = start
            previous_control = None
            previous_command = "Z"
            command = None
            continue
        if kind != "M" and not current:
            # A drawing command straight after a closepath starts at the subpath's start.
            current = [position]
        if kind == "M":
            if current:
                polygons.append(current)
            position = (ox + number(), oy + number())
            start = position
            current = [position]
            previous_control = None
        elif kind == "L":
            position = (ox + number(), oy + number())
            current.append(position)
            previous_control = None
        elif kind == "H":
            position = ((ox if relative else 0.0) + number(), position[1])
            current.append(position)
            previous_control = None
        elif kind == "V":
            position = (position[0], (oy if relative else 0.0) + number())
            current.append(position)
            previous_control = None
        elif kind == "C":
            c1 = (ox + number(), oy + number())
            c2 = (ox + number(), oy + number())
            end = (ox + number(), oy + number())
            current.extend(_cubic(position, c1, c2, end))
            previous_control, position = c2, end
        elif kind == "S":
            if previous_command.upper() in ("C", "S") and previous_control is not None:
                c1 = (2 * position[0] - previous_control[0], 2 * position[1] - previous_control[1])
            else:
                c1 = position
            c2 = (ox + number(), oy + number())
            end = (ox + number(), oy + number())
            current.extend(_cubic(position, c1, c2, end))
            previous_control, position = c2, end
        elif kind == "Q":
            c1 = (ox + number(), oy + number())
            end = (ox + number(), oy + number())
            current.extend(_quadratic(position, c1, end))
            previous_control, position = c1, end
        elif kind == "T":
            if previous_command.upper() in ("Q", "T") and previous_control is not None:
                c1 = (2 * position[0] - previous_control[0], 2 * position[1] - previous_control[1])
            else:
                c1 = position
            end = (ox + number(), oy + number())
            current.extend(_quadratic(position, c1, end))
            previous_control, position = c1, end
        elif kind == "A":
            rx, ry, rotation = number(), number(), number()
            large = _flag(tokens, index)
            index += 1
            sweep = _flag(tokens, index)
            index += 1
            end = (ox + number(), oy + number())
            current.extend(_arc(position, rx, ry, rotation, large, sweep, end))
            position = end
            previous_control = None
        else:  # pragma: no cover - COMMAND admits nothing else
            raise IconError(f"unknown path command {command!r}")
        previous_command = command
    if current:
        polygons.append(current)
    return polygons


def svg_polygons(svg: bytes) -> tuple[list[list[tuple[float, float]]], tuple[float, float]]:
    """Every path of an icon as polygons, and the size of its view box."""
    root = ElementTree.fromstring(svg)
    view_box = root.attrib.get("viewBox", "0 0 24 24").replace(",", " ")
    box = [float(part) for part in view_box.split()]
    if len(box) != 4 or box[0] or box[1]:
        raise IconError(f"a view box this tool does not handle: {box}")
    polygons = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "path":
            if "transform" in element.attrib or element.attrib.get("fill-rule") == "evenodd":
                raise IconError("a path with a transform or an even-odd fill")
            polygons.extend(path_polygons(element.attrib.get("d", "")))
    return polygons, (box[2], box[3])


# ------------------------------------------------------------------ rasterise --


def coverage(polygons, box, size) -> list[list[float]]:
    """The fraction of each pixel the shape covers, non-zero rule, rows top to bottom."""
    scale_x, scale_y = size / box[0], size / box[1]
    edges = []
    for polygon in polygons:
        points = [(x * scale_x, y * scale_y) for x, y in polygon]
        for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
            if y0 != y1:
                edges.append((x0, y0, x1, y1))
    rows = []
    for row in range(size):
        cells = [0.0] * (size + 1)
        for sub in range(SUBSCANLINES):
            y = row + (sub + 0.5) / SUBSCANLINES
            crossings = []
            for x0, y0, x1, y1 in edges:
                if y0 <= y < y1:
                    crossings.append((x0 + (y - y0) * (x1 - x0) / (y1 - y0), 1))
                elif y1 <= y < y0:
                    crossings.append((x0 + (y - y0) * (x1 - x0) / (y1 - y0), -1))
            crossings.sort()
            winding = 0
            for (x, direction), following in zip(crossings, crossings[1:] + [None]):
                winding += direction
                if winding == 0 or following is None:
                    continue
                left, right = max(0.0, x), min(float(size), following[0])
                if right <= left:
                    continue
                first, last = int(left), min(size - 1, int(right))
                if first == last:
                    cells[first] += right - left
                    continue
                cells[first] += first + 1 - left
                for column in range(first + 1, last):
                    cells[column] += 1.0
                cells[last] += right - last
        rows.append([min(1.0, cell / SUBSCANLINES) for cell in cells[:size]])
    return rows


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))


def png_rgba(alpha_rows, colour=(255, 255, 255)) -> bytes:
    """An 8-bit RGBA PNG of one colour, the shape in its alpha. No time, no text chunk."""
    height = len(alpha_rows)
    width = len(alpha_rows[0]) if height else 0
    raw = bytearray()
    for row in alpha_rows:
        raw.append(0)  # filter type None: the bytes are the pixels
        for value in row:
            raw.extend(colour)
            raw.append(value)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b""))


def read_png_alpha(data: bytes) -> list[list[int]]:
    """The alpha channel of a PNG this tool wrote (8-bit RGBA, filter None) - for checks."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise IconError("not a PNG")
    position = 8
    width = height = None
    body = b""
    while position < len(data):
        (length,) = struct.unpack(">I", data[position:position + 4])
        kind = data[position + 4:position + 8]
        content = data[position + 8:position + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour_type = struct.unpack(">IIBB", content[:10])
            if (depth, colour_type) != (8, 6):
                raise IconError("not an 8-bit RGBA PNG")
        elif kind == b"IDAT":
            body += content
        position += 12 + length
    raw = zlib.decompress(body)
    stride = 1 + 4 * width
    rows = []
    for row in range(height):
        line = raw[row * stride:(row + 1) * stride]
        if line[0] != 0:
            raise IconError("a filtered row; this reader takes only what this tool writes")
        rows.append([line[1 + 4 * column + 3] for column in range(width)])
    return rows


def parse_colour(text: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})", text)
    if not match:
        raise IconError(f"a colour this tool does not read: {text!r}")
    return tuple(int(part, 16) for part in match.groups())


def render_svg(svg: bytes, size: int, colour=(255, 255, 255)) -> bytes:
    polygons, box = svg_polygons(svg)
    rows = coverage(polygons, box, size)
    return png_rgba([[int(round(value * 255)) for value in row] for row in rows], colour)


def notice_text(manifest: dict) -> str:
    """The attribution that ships with the PNGs, from the manifest: which icon is which."""
    source = manifest["source"]
    lines = [
        "The icons in this directory",
        "===========================",
        "",
        "Each PNG here is a white rendering, 64x64 pixels, of one icon of Material Design",
        "Icons by Pictogrammers (https://pictogrammers.com), taken from the npm package",
        f"{source['package']} {source['version']} ({source['git_repository']}, "
        f"{source['git_tag']} {source['git_commit']}).",
        "",
        "The icons are licensed under the Apache License, Version 2.0; the text is in",
        "LICENSE-Apache-2.0.txt beside this file. They were rendered to PNG for this plugin",
        "(tools/render-icons.py in the plugin's source repository) and are otherwise unchanged.",
        "No brand icon is among them.",
        "",
        "The plugin is GPL-2.0-or-later. Apache-2.0 is compatible with version 3 of the GNU",
        "General Public License and not with version 2, so the package that carries these icons",
        "is distributed under the terms of the GNU General Public License version 3 or later.",
        "",
        "Name in the menu    Material Design Icons icon        Author",
    ]
    for name, entry in sorted(manifest["icons"].items()):
        lines.append(f"{name:<19} {entry['mdi']:<33} {entry.get('author') or '-'}")
    return "\n".join(line.rstrip() for line in lines) + "\n"


def render_all(manifest: dict, check: bool = False, directory: Path = PNG_DIRECTORY):
    """Render every icon; with `check`, compare instead and return the names that differ."""
    size = int(manifest["render"]["size"])
    colour = parse_colour(manifest["render"]["colour"])
    differ = []
    notice = notice_text(manifest).encode("utf-8")
    if check:
        target = directory / "NOTICE"
        if not target.exists() or target.read_bytes() != notice:
            differ.append("NOTICE")
    else:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "NOTICE").write_bytes(notice)
    for name, entry in sorted(manifest["icons"].items()):
        svg = (SVG_DIRECTORY / (entry["mdi"] + ".svg")).read_bytes()
        png = render_svg(svg, size, colour)
        target = directory / (name + ".png")
        if check:
            if not target.exists() or target.read_bytes() != png:
                differ.append(name)
        else:
            directory.mkdir(parents=True, exist_ok=True)
            target.write_bytes(png)
    return differ


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="take the SVGs out of the @mdi/svg archive")
    importer.add_argument("archive", type=Path)
    renderer = commands.add_parser("render", help="draw the PNGs from the SVGs")
    renderer.add_argument("--check", action="store_true",
                          help="compare with the committed PNGs instead of writing them")
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            manifest = import_archive(args.archive)
            print(f"imported {len(manifest['icons'])} icons from "
                  f"{manifest['source']['package']} {manifest['source']['version']}")
            return 0
        manifest = read_manifest()
        differ = render_all(manifest, check=args.check)
        if args.check:
            for name in differ:
                print(f"{name} differs from what the manifest renders to")
            print("every icon matches its SVG" if not differ else f"{len(differ)} differ")
            return 1 if differ else 0
        where = PNG_DIRECTORY.relative_to(REPO_ROOT)
        print(f"rendered {len(manifest['icons'])} icons into {where}")
        return 0
    except IconError as error:
        print(f"render-icons: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
