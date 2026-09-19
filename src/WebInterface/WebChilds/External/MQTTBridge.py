"""OpenWebif compatibility hook for the optional MQTT Bridge page.

This file is what OpenWebif looks for. With no original WebInterface plugin
installed, OpenWebif symlinks its own `pluginshook.src` in as
`WebInterface/WebChilds/Toplevel.py`, loads every module in this directory by
path, and then mounts what they registered:

    from Plugins.Extensions.WebInterface.WebChilds.Toplevel import loaded_plugins
    for plugin in loaded_plugins:
        root.putChild2(plugin[0], plugin[1])

`addExternalChild` takes **one sequence** of at least three items — link,
resource, name, and by convention version, has-GUI and web target — refuses a
link another plugin already registered, and appends it to `loaded_plugins`.
The link is a **`str`**: `putChild2` is the thing that encodes it
(`six.ensure_binary`), and OpenWebif also formats it into its log line, where
a bytes object would read `b'mqttbridge'`. Twisted's own `putChild` inside the
resource does need bytes, which is why the page's icon child is registered as
`b"icon"` and not as `"icon"`.
"""

try:
    from Plugins.Extensions.MQTTBridge.webif import create_resource
    from Plugins.Extensions.WebInterface.WebChilds.Toplevel import addExternalChild

    addExternalChild(("mqttbridge", create_resource(), "MQTT Bridge", 1, True, "_self"))
except Exception:
    # OpenWebif is optional. Its absence must never stop Enigma2 or MQTT Bridge.
    pass
