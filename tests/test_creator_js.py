"""The menu creator's script, `creator.js`: what it may never contain, and its own tests in Node.

The one script this plugin serves (ADR-0022), and the page it runs on shows text anyone on the
broker can write. So two things are held here. Statically, on the file as it ships: no way of
writing markup, no code built from text, no address of another origin, plain ASCII. And by
behaviour: `tests/js/creator_test.js` runs the script in Node against a small fake document that
refuses those same things, and checks the editing, the bounds and the saves.

The Node run is skipped on a machine without Node, except in CI (`CI` is set there): a CI runner
without Node fails this test instead of passing it silently.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "src" / "MQTTBridge" / "creator.js"
NODE_TEST = REPO_ROOT / "tests" / "js" / "creator_test.js"

FORBIDDEN = [
    "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "Function(",
    "setTimeout(\"", "setTimeout('", "setInterval(", "srcdoc", "javascript:", "import(",
    "XMLHttpRequest", "localStorage", "sessionStorage", "postMessage", "window.open",
]


def source():
    return SCRIPT.read_text(encoding="ascii")


@pytest.mark.parametrize("construct", FORBIDDEN)
def test_the_script_never_uses(construct):
    assert construct not in source()


def test_the_script_names_no_other_origin():
    assert re.search(r"https?:|//[a-z0-9.-]+\.[a-z]{2,}", source()) is None


def test_set_attribute_is_only_ever_given_constant_text():
    for call in re.findall(r"setAttribute\(([^)]*)\)", source()):
        assert re.fullmatch(r'"[a-z-]+", "[a-z ]+"', call), call


def test_the_script_is_plain_ascii_with_no_invisible_characters():
    data = SCRIPT.read_bytes()
    assert all(byte < 0x80 for byte in data)
    assert b"\r" not in data


def test_the_script_is_small():
    assert len(SCRIPT.read_bytes()) < 48 * 1024


def test_the_script_is_strict_and_wrapped():
    text = source()
    assert '"use strict";' in text
    assert text.lstrip().startswith("/*")
    assert text.rstrip().endswith("}(typeof window === \"undefined\" ? null : window));")


def test_the_script_behaves_in_node():
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("CI has no Node, so the menu creator's own tests did not run")
        pytest.skip("no Node on this machine")
    result = subprocess.run([node, str(NODE_TEST)], capture_output=True, text=True, timeout=120,
                            check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"^\d+ tests, 0 failed$", result.stdout, re.M), result.stdout
