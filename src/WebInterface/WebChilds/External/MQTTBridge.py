"""OpenWebif compatibility hook for the optional MQTT Bridge page."""

try:
    from Plugins.Extensions.MQTTBridge.webif import create_resource
    from Plugins.Extensions.WebInterface.WebChilds.Toplevel import addExternalChild

    addExternalChild(("mqttbridge", create_resource(), "MQTT Bridge", 1, True, "_self"))
except Exception:
    # OpenWebif is optional. Its absence must never stop Enigma2 or MQTT Bridge.
    pass
