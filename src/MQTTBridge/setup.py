"""The setup screen under *Menu → Plugins → MQTT Bridge*.

Built on `ConfigListScreen` and nothing else. `Screens.Setup` needs an entry in
the image's `setup.xml`, which not every image will accept from a third-party
plugin and which some images rearrange between releases; `ConfigListScreen` has
been in every enigma2 flavour for a decade and needs no registration at all.

The status line is the point of the screen as much as the fields are: a box that
is idle because the broker address is empty looks exactly like a box that cannot
reach the broker, unless somebody says which it is.
"""

from Components.ActionMap import ActionMap
from Components.config import config, configfile, getConfigListEntry
from Components.ConfigList import ConfigListScreen
from Components.Label import Label
from Screens.Screen import Screen

from .i18n import _
from .log import get_logger

LOG = get_logger("setup")


def setting_labels():
    """Setting name, and the label a household sees. The order is the screen's."""
    return (
        ("enabled", _("Enabled")),
        ("host", _("Broker address")),
        ("port", _("Broker port")),
        ("tls", _("Use TLS")),
        ("ca_file", _("CA certificate file")),
        ("username", _("Broker username")),
        ("password", _("Broker password")),
        ("node_id", _("Node id")),
        ("friendly_name", _("Device name")),
        ("base_topic", _("Base topic")),
        ("ha_discovery_prefix", _("Home Assistant discovery prefix")),
        ("ha_mode", _("Home Assistant mode")),
        ("publish_keys", _("Publish remote key presses")),
        ("screenshot", _("Screenshots")),
        ("screenshot_interval", _("Screenshot interval (seconds)")),
        ("bouquets_for_select", _("Bouquets for the channel list")),
        ("deep_standby_allowed", _("Allow deep standby and reboot")),
        ("log_level", _("Log level")),
        ("epg_grid_events", _("EPG grid events per channel")),
    )


def build_entries(settings):
    entries = []
    for name, label in setting_labels():
        element = getattr(settings, name, None)
        if element is None:
            LOG.warning("no such setting: %s", name)
            continue
        entries.append(getConfigListEntry(label, element))
    return entries


def status_text(bridge, settings):
    node = (settings.node_id.value or "").strip()
    if not settings.enabled.value:
        state = _("Disabled")
    elif bridge is None:
        state = _("Disconnected")
    elif bridge.idle_reason:
        state = _("Idle")
    elif bridge.connected:
        state = _("Connected")
    else:
        state = _("Disconnected")
    return state + "  ·  " + (_("Node: %s") % (node or "-"))


class MQTTBridgeSetup(Screen, ConfigListScreen):
    skin = """
        <screen name="MQTTBridgeSetup" position="center,center" size="900,600" title="MQTT Bridge">
            <widget name="config" position="20,20" size="860,470" scrollbarMode="showOnDemand" />
            <widget name="status" position="20,500" size="860,30" font="Regular;22" />
            <widget name="key_red" position="20,550" size="200,30" font="Regular;22"
                    foregroundColor="red" />
            <widget name="key_green" position="230,550" size="200,30" font="Regular;22"
                    foregroundColor="green" />
        </screen>
    """

    def __init__(self, session, settings=None, bridge=None):
        Screen.__init__(self, session)
        self.settings = settings if settings is not None else config.plugins.mqttbridge
        self._bridge = bridge

        self.entries = build_entries(self.settings)
        ConfigListScreen.__init__(self, self.entries, session=session)

        self["status"] = Label(status_text(self._bridge_or_running(), self.settings))
        self["key_red"] = Label(_("Cancel"))
        self["key_green"] = Label(_("Save"))
        self["mqttbridgeActions"] = ActionMap(
            ["SetupActions", "ColorActions"],
            {
                "save": self.keySave,
                "green": self.keySave,
                "cancel": self.keyCancel,
                "red": self.keyCancel,
            },
            -2,
        )
        try:
            # Not every flavour of Screen has it, and none of them need it.
            self.setTitle(_("MQTT Bridge"))
        except Exception:
            LOG.debug("this image's Screen has no usable setTitle")

    def _bridge_or_running(self):
        if self._bridge is not None:
            return self._bridge
        try:
            from .plugin import get_bridge

            return get_bridge()
        except Exception:
            return None

    def keySave(self):
        for entry in self.entries:
            element = entry[1]
            try:
                element.save()
            except Exception:
                LOG.exception("could not save a setting")
        try:
            configfile.save()
        except Exception:
            LOG.exception("could not write enigma2's settings file")

        bridge = self._bridge_or_running()
        if bridge is not None:
            try:
                bridge.reload()
            except Exception:
                LOG.exception("the bridge could not be restarted with the new settings")
        self.close(True)

    def keyCancel(self):
        for entry in self.entries:
            element = entry[1]
            try:
                element.cancel()
            except Exception:
                LOG.exception("could not restore a setting")
        self.close(False)
