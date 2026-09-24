"""OpenWebif compatibility hook for the optional MQTT Bridge page.

This file is what OpenWebif looks for. With no original WebInterface plugin
installed, OpenWebif symlinks its own `pluginshook.src` in as
`WebInterface/WebChilds/Toplevel.py`, loads every module in this directory by
path, and then mounts what they registered:

    from Plugins.Extensions.WebInterface.WebChilds.Toplevel import loaded_plugins
    for plugin in loaded_plugins:
        root.putChild2(plugin[0], plugin[1])

`addExternalChild` takes **one sequence** of at least three items - link,
resource, name, and by convention version, has-GUI and web target - refuses a
link another plugin already registered, and appends it to `loaded_plugins`.
The link is a **`str`**: `putChild2` is the thing that encodes it
(`six.ensure_binary`), and OpenWebif also formats it into its log line, where
a bytes object would read `b'mqttbridge'`. Twisted's own `putChild` inside the
resource does need bytes, which is why the page's icon child is registered as
`b"icon"` and not as `"icon"`.

🔴 **The sixth item decides how OpenWebif's menu opens the page, and it is
`"_self"`.** Measured in the `prepareMainTemplate` OpenWebif 2.2 runs on
OpenViX 6.6: for each external child with a GUI (fifth item `True`), a sixth
item equal to `"_self"` becomes a menu entry that calls
`load_maincontent('<link>')`, which on the receiver is
`$("#content_container").load(url)` - the response is fetched by jQuery and
injected into OpenWebif's own content panel. Any other sixth item makes the
entry a plain `target='_blank'` link. 0.3.0 first chose `"_blank"` because a
whole page injected there leaks its CSS into OpenWebif and its forms navigate
the whole window. That reason holds for a whole page, and the page no longer
answers a panel load with one: it answers with a fragment holding an `<iframe>`
of itself and a link to a new tab (see `webif.py`, ADR-0010). So the entry opens
the page inside OpenWebif, like every other entry. (`fancontrol` and
`iptvplayer` are special-cased by name and do not apply.)

Mounting here is also what decides who reaches the page. OpenWebif wraps the
tree it mounts this child on in its own authentication - for HTTP and again for
HTTPS - and decides on the first path segment, before the page's code runs. The
page trusts the web interface that mounted it and enforces no login of its own
(ADR-0009). On an image with the original WebInterface installed instead,
OpenWebif does not install its loader and the page would sit under that
interface's authentication; that case has not been measured.
"""

try:
    from Plugins.Extensions.MQTTBridge.webif import create_resource
    from Plugins.Extensions.WebInterface.WebChilds.Toplevel import addExternalChild

    addExternalChild(("mqttbridge", create_resource(), "MQTT Bridge", 1, True, "_self"))
except Exception:
    # OpenWebif is optional. Its absence must never stop Enigma2 or MQTT Bridge.
    pass
