"""Translations for the strings a household sees.

Source strings are English. Topics, JSON keys and log lines stay English
everywhere — only what appears on the television is translated, the way
OpenWebif and the other OE-Alliance plugins do it: `bindtextdomain` against the
plugin's own `locale/` directory and `dgettext` for the lookup, so the catalogue
never collides with enigma2's own domain.

`_` degrades to the identity function when gettext or the catalogue is missing,
which is what happens on a developer's machine and in the tests.
"""

import gettext
import os

DOMAIN = "MQTTBridge"


def locale_directory():
    """Where the compiled catalogues live, on a box and off it."""
    try:
        from Tools.Directories import SCOPE_PLUGINS, resolveFilename

        resolved = resolveFilename(SCOPE_PLUGINS, "Extensions/MQTTBridge/locale")
        if resolved:
            return resolved
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")


def _bind():
    try:
        gettext.bindtextdomain(DOMAIN, locale_directory())
        return True
    except Exception:
        return False


_bound = _bind()


def _(text):
    """Translate a household-visible string."""
    if not _bound:
        return text
    try:
        return gettext.dgettext(DOMAIN, text)
    except Exception:
        return text
