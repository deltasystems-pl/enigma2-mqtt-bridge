"""The receiver menu's icons: the names are the contract, the files are rendered, the licence holds.

Every icon name of the contract (docs/TOPICS.md, "The icon names") has a 64x64 RGBA PNG in the
package, rendered by `tools/render-icons.py` from a Material Design Icons SVG under Apache-2.0
that `assets/icons/manifest.json` names. These tests hold the four together: the names in the
code, the manifest, the PNGs, and what the SVGs render to now.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from MQTTBridge.menu import ICON_NAMES, NEUTRAL_ICON

REPO_ROOT = Path(__file__).resolve().parents[1]
ICONS = REPO_ROOT / "src" / "MQTTBridge" / "icons"
ASSETS = REPO_ROOT / "assets" / "icons"
TOPICS = REPO_ROOT / "docs" / "TOPICS.md"


def _tool():
    spec = importlib.util.spec_from_file_location("render_icons",
                                                  REPO_ROOT / "tools" / "render-icons.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _tool()


@pytest.fixture(scope="module")
def manifest():
    return json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))


def test_the_names_are_the_manifests_and_the_documents(manifest):
    assert sorted(ICON_NAMES) == sorted(manifest["icons"])
    assert len(set(ICON_NAMES)) == len(ICON_NAMES)
    assert NEUTRAL_ICON in ICON_NAMES
    text = TOPICS.read_text(encoding="utf-8")
    paragraph = text.split("**The icon names**", 1)[1].split("\n\n", 1)[0]
    documented = re.findall(r"`([a-z-]+)`", paragraph.split(":", 1)[1])
    assert documented == list(ICON_NAMES)


def test_the_names_the_brief_asks_for_are_all_there():
    required = ("neutral lamp ceiling-light blinds scene script power fan lock radiator "
                "thermometer tv speaker door gate garage alarm camera sofa bed kitchen garden "
                "movie note home").split()
    assert set(required) <= set(ICON_NAMES)


@pytest.mark.parametrize("name", ICON_NAMES)
def test_every_icon_is_a_64_pixel_white_rgba_png(tool, name):
    data = (ICONS / (name + ".png")).read_bytes()
    assert data[12:16] == b"IHDR"
    alpha = tool.read_png_alpha(data)
    assert len(alpha) == 64 and all(len(row) == 64 for row in alpha)
    covered = sum(1 for row in alpha for value in row if value)
    assert 64 * 64 // 20 < covered < 64 * 64, "a glyph, neither empty nor a filled square"
    # No chunk that says when or on what it was made: the same SVG gives the same bytes.
    assert b"tIME" not in data and b"tEXt" not in data


@pytest.mark.parametrize("name", ICON_NAMES)
def test_every_icon_is_what_its_svg_renders_to(tool, manifest, name):
    entry = manifest["icons"][name]
    svg = (ASSETS / "mdi" / (entry["mdi"] + ".svg")).read_bytes()
    rendered = tool.read_png_alpha(tool.render_svg(svg, 64))
    committed = tool.read_png_alpha((ICONS / (name + ".png")).read_bytes())
    # One step of alpha is left to the platform's last bit of floating point.
    worst = max(abs(a - b) for row_a, row_b in zip(rendered, committed)
                for a, b in zip(row_a, row_b))
    assert worst <= 1


@pytest.mark.parametrize("name", ICON_NAMES)
def test_every_icon_is_apache_2_and_no_brand(manifest, name):
    entry = manifest["icons"][name]
    assert entry["licence"] == "Apache-2.0"
    assert "Brand / Logo" not in entry["tags"]
    assert entry["author"]
    assert (ASSETS / "mdi" / (entry["mdi"] + ".svg")).is_file()


def test_the_source_is_pinned(manifest):
    source = manifest["source"]
    assert source["package"] == "@mdi/svg"
    assert re.fullmatch(r"\d+\.\d+\.\d+", source["version"])
    assert source["integrity"].startswith("sha512-")
    assert re.fullmatch(r"[0-9a-f]{40}", source["git_commit"])


def test_the_licence_texts_ship_with_the_icons():
    apache = (ICONS / "LICENSE-Apache-2.0.txt").read_text(encoding="utf-8")
    assert "Apache License" in apache and "Version 2.0, January 2004" in apache
    upstream = (ASSETS / "mdi" / "LICENSE").read_text(encoding="utf-8")
    assert "Icons: Apache 2.0" in upstream
    notice = (ICONS / "NOTICE").read_text(encoding="utf-8")
    assert "Material Design Icons" in notice and "Apache License, Version 2.0" in notice
    root_notice = (REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "Material Design Icons" in root_notice
    assert "GNU General Public License version 3" in root_notice


def test_the_rule_refuses_a_brand_a_deprecated_and_an_unknown_icon(tool):
    assert tool.licence_problems("x", None) == ["x: not in the package's meta.json"]
    assert tool.licence_problems("x", {"tags": ["Brand / Logo"], "deprecated": False})
    assert tool.licence_problems("x", {"tags": [], "deprecated": True})
    assert tool.licence_problems("x", {"tags": ["Home Automation"], "deprecated": False}) == []


def test_the_rasteriser_fills_by_the_non_zero_rule(tool):
    """A square with a hole drawn the other way round is a ring; the same way round, solid."""
    ring = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            b'<path d="M2,2H22V22H2Z M8,8V16H16V8Z"/></svg>')
    solid = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
             b'<path d="M2,2H22V22H2Z M8,8H16V16H8Z"/></svg>')
    centre = 32
    assert tool.read_png_alpha(tool.render_svg(ring, 64))[centre][centre] == 0
    assert tool.read_png_alpha(tool.render_svg(solid, 64))[centre][centre] == 255


def test_the_rasteriser_reads_arcs_and_relative_curves(tool):
    circle = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
              b'<path d="M12,2A10,10 0 0,1 22,12A10,10 0 0,1 12,22A10,10 0 0,1 2,12'
              b'A10,10 0 0,1 12,2Z"/></svg>')
    alpha = tool.read_png_alpha(tool.render_svg(circle, 64))
    assert alpha[32][32] == 255
    assert alpha[1][1] == 0
    covered = sum(value for row in alpha for value in row) / 255.0
    expected = 3.14159 * (10 * 64 / 24) ** 2
    assert abs(covered - expected) / expected < 0.01
    curve = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
             b'<path d="m2 2c10 0 20 10 20 20h-20z"/></svg>')
    alpha = tool.read_png_alpha(tool.render_svg(curve, 64))
    assert alpha[56][8] == 255, "inside, near the bottom left"
    assert alpha[8][56] == 0, "outside the curve, at the top right"
