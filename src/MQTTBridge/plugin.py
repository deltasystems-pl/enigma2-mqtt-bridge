"""What enigma2 loads.

Every import of an enigma2 name in this file is guarded, and every entry point
swallows what it catches after writing one line to the log. The reason is blunt:
this module runs inside the process that draws the television, and a plugin that
raises during start-up can leave a receiver with no user interface and its owner
with no way to fix it except a serial console.

So an image that does not provide something the bridge wants gets a plugin that
loads, appears in the menu, and does nothing.

Three descriptors:

* `WHERE_SESSIONSTART` - the only one that is handed the `session`, which every
  screen and every enigma2 event source hangs off;
* `WHERE_AUTOSTART` with reason 1 - the shutdown edge, where the bridge gets to
  say `offline` properly instead of leaving it to the last will;
* `WHERE_PLUGINMENU` - the setup screen.

And one more while `menu_allowed` is on: `WHERE_EXTENSIONSMENU`, "Home Assistant", which opens
the receiver menu (`menu.py`). The setting can change while enigma2 runs, so the entry is also
added to and taken out of the image's plugin list at run time (`sync_menu_entry`).
"""

from .i18n import _
from .log import get_logger

LOG = get_logger("plugin")

PLUGIN_NAME = "MQTT Bridge"
PLUGIN_ICON = "plugin.png"
MENU_NAME = "Home Assistant"

_bridge = None
# The Extensions entry: one object for the life of the process, because the image's plugin list
# finds a descriptor by its function, and adding a second would list the menu twice.
_menu_entry = None


def get_bridge():
    """The running bridge, or None. The setup screen asks so it can show a status."""
    return _bridge


def sessionstart(reason, **kwargs):
    """reason 0: enigma2 has a session. Start the bridge."""
    global _bridge
    if reason != 0:
        return
    try:
        from .bridge import Bridge
    except Exception:
        LOG.exception("the bridge could not be imported; the plugin stays idle")
        return
    try:
        if _bridge is None:
            _bridge = Bridge(session=kwargs.get("session"))
        _bridge.start()
    except Exception:
        LOG.exception("the bridge could not be started; the plugin stays idle")


def autostart(reason, **kwargs):
    """reason 1: enigma2 is going away. Disconnect before it does."""
    if reason != 1:
        return
    try:
        if _bridge is not None:
            _bridge.stop()
    except Exception:
        LOG.exception("the bridge could not be stopped cleanly")


def open_setup(session, **kwargs):
    try:
        updater = getattr(_bridge, "self_update", None)
        if updater is not None and updater.closed:
            # An update has put the new release on disk under this process: the setup
            # screen would be a first import of new code, and a save would reopen a session.
            from Screens.MessageBox import MessageBox

            from .selfupdate import household_doors

            session.open(MessageBox, household_doors(updater),
                         getattr(MessageBox, "TYPE_INFO", 1))
            return
        from .setup import MQTTBridgeSetup

        session.open(MQTTBridgeSetup)
    except Exception:
        LOG.exception("the setup screen could not be opened")


def open_menu(session, **kwargs):
    """The Extensions entry: the receiver menu, or a sentence saying why it cannot open."""
    try:
        menu = _bridge.publisher("menu") if _bridge is not None else None
        if menu is not None and menu.claimed() and menu.open(session):
            return
        from Screens.MessageBox import MessageBox

        if _bridge is None or not _bridge.running:
            text = _("The Home Assistant menu is not available: the plugin is not running.")
        else:
            text = _("The Home Assistant menu cannot be opened on this receiver now.")
        session.open(MessageBox, text, getattr(MessageBox, "TYPE_INFO", 1), timeout=10)
    except Exception:
        LOG.exception("the receiver menu could not be opened")


def menu_entry():
    """The Extensions entry's descriptor, built once; None where the image has no descriptors."""
    global _menu_entry
    if _menu_entry is None:
        from Plugins.Plugin import PluginDescriptor

        _menu_entry = PluginDescriptor(
            name=_(MENU_NAME),
            description=_("The menu Home Assistant sends to this receiver"),
            where=PluginDescriptor.WHERE_EXTENSIONSMENU,
            fnc=open_menu,
            needsRestart=False,
        )
    return _menu_entry


def _menu_allowed():
    try:
        from .config import value

        return bool(value("menu_allowed"))
    except Exception:
        LOG.exception("menu_allowed could not be read; the menu entry is left out")
        return False


def sync_menu_entry(allowed):
    """Put the Extensions entry into the image's plugin list, or take it out. Never raises."""
    try:
        from Components.PluginComponent import plugins
    except Exception as error:
        from .enigma2 import missing

        missing("Components.PluginComponent", error)
        return False
    try:
        entry = menu_entry()
        present = entry in list(getattr(plugins, "pluginList", []) or [])
        if allowed and not present:
            plugins.addPlugin(entry)
        elif not allowed and present:
            plugins.removePlugin(entry)
        return True
    except Exception:
        LOG.exception("the menu's Extensions entry could not be changed")
        return False


def Plugins(**kwargs):
    try:
        from Plugins.Plugin import PluginDescriptor
    except Exception:
        LOG.exception("this image has no Plugins.Plugin; the plugin cannot load")
        return []

    try:
        return [
            PluginDescriptor(
                name=PLUGIN_NAME,
                description=_("Publish receiver state to MQTT and take commands back."),
                where=PluginDescriptor.WHERE_SESSIONSTART,
                fnc=sessionstart,
                needsRestart=False,
            ),
            PluginDescriptor(
                name=PLUGIN_NAME,
                description=_("Publish receiver state to MQTT and take commands back."),
                where=PluginDescriptor.WHERE_AUTOSTART,
                fnc=autostart,
                needsRestart=False,
            ),
            PluginDescriptor(
                name=_(PLUGIN_NAME),
                description=_("Publish receiver state to MQTT and take commands back."),
                where=PluginDescriptor.WHERE_PLUGINMENU,
                icon=PLUGIN_ICON,
                fnc=open_setup,
                needsRestart=False,
            ),
        ] + ([menu_entry()] if _menu_allowed() else [])
    except Exception:
        LOG.exception("the plugin descriptors could not be built")
        return []
