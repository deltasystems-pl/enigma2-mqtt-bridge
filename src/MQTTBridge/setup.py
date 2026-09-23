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
from Screens.MessageBox import MessageBox
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
        ("screenshot_delay", _("Screenshot delay after a zap (seconds)")),
        ("cam_telemetry", _("Publish conditional-access telemetry")),
        ("oscam_telemetry", _("Publish OSCam server telemetry")),
        ("oscam_port", _("OSCam WebIf port")),
        ("oscam_username", _("OSCam WebIf username")),
        ("oscam_password", _("OSCam WebIf password")),
        ("bouquets_for_select", _("Bouquets for the channel list")),
        ("deep_standby_allowed", _("Allow deep standby and reboot")),
        ("cec_standby_workaround", _("Close the channel list when the TV asks for standby")),
        ("softcam_restart_allowed", _("Allow restarting the softcam")),
        ("softcam_autoheal", _("Restart the softcam when it stops decoding")),
        ("softcam_autoheal_seconds", _("Wait before an automatic softcam restart (seconds)")),
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
        try:
            ConfigListScreen.__init__(
                self, self.entries, session=session, on_change=self._entry_changed
            )
        except TypeError:
            # Older images' ConfigListScreen takes no `on_change`. The screen
            # still works; its status line simply stops refreshing as fields are
            # edited, which is cosmetic.
            LOG.debug("this image's ConfigListScreen has no on_change")
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

    def _entry_changed(self):
        """Keep the status line honest while the fields are being edited."""
        try:
            self["status"].setText(status_text(self._bridge_or_running(), self.settings))
        except Exception:
            # Called once before the label exists on some images, and a status
            # line is never worth an exception in front of a user.
            LOG.debug("the status line could not be refreshed")

    def _anything_changed(self):
        for entry in self.entries:
            element = entry[1]
            checker = getattr(element, "isChanged", None)
            try:
                # enigma2 keeps `saved_value` as a string, so only the element
                # itself can answer this; the comparison is the fallback for a
                # stub or an image that does not offer `isChanged`.
                changed = bool(checker()) if checker is not None else (
                    element.value != element.saved_value
                )
            except Exception:
                changed = element.value != element.saved_value
            if changed:
                return True
        return False

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
        """Red or Exit. Ask first when there is something to lose.

        A broker password typed on a remote control with the on-screen keyboard
        is minutes of somebody's evening, and Exit is next to the arrow keys.
        The question is the image's own standard one, so it reads like the rest
        of the receiver.
        """
        if not self._anything_changed():
            self._discard()
            return
        try:
            self.session.openWithCallback(
                self._cancel_answered,
                MessageBox,
                _("Really close without saving settings?"),
                getattr(MessageBox, "TYPE_YESNO", 0),
            )
        except Exception:
            # No dialog on this image is not a reason to trap the user in the
            # screen: fall back to the old behaviour.
            LOG.exception("could not ask about the unsaved settings")
            self._discard()

    def _cancel_answered(self, confirmed):
        if confirmed:
            self._discard()

    def _discard(self):
        for entry in self.entries:
            element = entry[1]
            try:
                element.cancel()
            except Exception:
                LOG.exception("could not restore a setting")
        self.close(False)
