"""The translation catalogues.

A household-visible string that never reached the template is a string that can
never be translated, and nobody notices until a Polish user reads an English
label. The check is cheap: every literal passed to `_()` in the package must
appear in the template, and both catalogues must cover the template exactly.
"""

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = REPO_ROOT / "src" / "MQTTBridge"
LOCALE = PACKAGE / "locale"
POT = LOCALE / "MQTTBridge.pot"
LANGUAGES = ("pl", "de")

MSGID = re.compile(r'^msgid\s+"(.*)"\s*$', re.M)
MSGSTR = re.compile(r'^msgstr\s+"(.*)"\s*$', re.M)


def catalogue(path):
    """msgid -> msgstr, header entry dropped."""
    text = path.read_text(encoding="utf-8")
    ids = MSGID.findall(text)
    strings = MSGSTR.findall(text)
    assert len(ids) == len(strings), path
    return {
        _unescape(key): _unescape(value)
        for key, value in zip(ids, strings)
        if key
    }


def _unescape(text):
    return text.replace('\\"', '"').replace("\\\\", "\\")


def translated_literals():
    """Every string literal handed to `_()` anywhere in the package."""
    found = set()
    for source in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "_"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str):
                    found.add(node.args[0].value)
    return found


def test_the_template_exists_and_is_not_empty():
    assert POT.exists()
    assert catalogue(POT)


def test_every_translated_literal_is_in_the_template():
    missing = translated_literals() - set(catalogue(POT))
    assert not missing, "not in MQTTBridge.pot: " + repr(sorted(missing))


def test_the_template_has_no_translations_in_it():
    assert set(catalogue(POT).values()) == {""}


def test_both_catalogues_exist_and_cover_the_template():
    template = set(catalogue(POT))
    for language in LANGUAGES:
        path = LOCALE / language / "LC_MESSAGES" / "MQTTBridge.po"
        assert path.exists(), path
        entries = catalogue(path)
        assert set(entries) == template, language
        empty = [key for key, value in entries.items() if not value.strip()]
        assert not empty, language + " has untranslated strings: " + repr(empty)


def test_the_polish_catalogue_reads_as_polish():
    entries = catalogue(LOCALE / "pl" / "LC_MESSAGES" / "MQTTBridge.po")
    assert entries["Broker password"] == "Hasło brokera"
    assert entries["Save"] == "Zapisz"
    # Diacritics, not their ASCII shadows.
    assert any("ł" in value or "ą" in value or "ż" in value for value in entries.values())


def test_the_german_catalogue_is_flagged_for_review():
    """Drafted, not confirmed. CONTRIBUTING.md asks for a native speaker."""
    text = (LOCALE / "de" / "LC_MESSAGES" / "MQTTBridge.po").read_text(encoding="utf-8")
    assert text.startswith("# needs-review")


def test_a_format_placeholder_survives_translation():
    template = catalogue(POT)
    assert "Node: %s" in template
    for language in LANGUAGES:
        entries = catalogue(LOCALE / language / "LC_MESSAGES" / "MQTTBridge.po")
        assert entries["Node: %s"].count("%s") == 1, language


def test_the_catalogues_declare_utf_8():
    for path in [POT] + [LOCALE / lang / "LC_MESSAGES" / "MQTTBridge.po" for lang in LANGUAGES]:
        assert "charset=UTF-8" in path.read_text(encoding="utf-8"), path


def test_the_fallback_translator_is_the_identity():
    """No catalogue, no crash - this runs on a PC in every test above."""
    from MQTTBridge.i18n import _

    assert _("Save") in ("Save", "Zapisz", "Speichern")
