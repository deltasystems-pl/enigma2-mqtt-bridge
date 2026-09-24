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
"""

from .i18n import _
from .log import get_logger

LOG = get_logger("plugin")

PLUGIN_NAME = "MQTT Bridge"
PLUGIN_ICON = "plugin.png"

_bridge = None


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
        from .setup import MQTTBridgeSetup

        session.open(MQTTBridgeSetup)
    except Exception:
        LOG.exception("the setup screen could not be opened")


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
        ]
    except Exception:
        LOG.exception("the plugin descriptors could not be built")
        return []
