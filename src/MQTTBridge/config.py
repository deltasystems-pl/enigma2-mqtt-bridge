"""The plugin's settings, and the provisioning file that fills them in.

Settings live in `config.plugins.mqttbridge.*`, which enigma2 persists in its own
`/etc/enigma2/settings`. There is no second configuration file to keep in step
and settings survive a plugin upgrade, because the package does not own them.

Every key the contract names exists here from the first release, including the
ones the current milestone does not read yet: the setup screen and the
provisioning file are how a box is configured, and a key that appears later
would be a key an installer could not have written.
"""

import json
import os
import re
import unicodedata

from Components.config import (
    ConfigInteger,
    ConfigPassword,
    ConfigSelection,
    ConfigSubsection,
    ConfigText,
    ConfigYesNo,
    config,
    configfile,
)

from .i18n import _
from .log import get_logger

LOG = get_logger("config")

PROVISIONING_PATH = "/etc/enigma2/mqttbridge.json"

HA_MODES = ("discovery", "integration", "off")
LOG_LEVELS = ("error", "warning", "info", "debug")
SCREENSHOT_MODES = ("off", "on_zap", "interval")
REMOTE_SETTING_NAMES = (
    "publish_keys",
    "screenshot",
    "screenshot_interval",
    "screenshot_delay",
    "cam_telemetry",
    "oscam_telemetry",
    # Not a permission: these two only *tune* a restart the box has already been
    # told it may perform, which is why they are here and `softcam_restart_allowed`
    # is in the read-only list below.
    "softcam_autoheal",
    "softcam_autoheal_seconds",
)
# Published in `info.settings` and refused by `cmd/config`, like any other key
# that is not in the allowlist above. A setting that *enables* a command is
# never writable over MQTT: it is set on the receiver - its setup screen, the
# provisioning file, or the OpenWebif page, which is exactly as open as the
# receiver's web interface (ADR-0009). Echoing it lets a consumer hide a control
# the box would always refuse instead of offering one that fails.
READ_ONLY_SETTING_NAMES = (
    "deep_standby_allowed", "softcam_restart_allowed", "epg_import_allowed",
    "uninstall_allowed",
)
SCREENSHOT_INTERVAL_LIMITS = (5, 3600)
SCREENSHOT_DELAY_LIMITS = (1, 30)
# Below thirty seconds the detector would be reading noise: a healthy encrypted
# channel refreshes its ECM file about every ten, and the spread is nine to
# eleven. Above ten minutes it is no longer a repair.
SOFTCAM_AUTOHEAL_LIMITS = (30, 600)

DEFAULT_BASE_TOPIC = "enigma2"
DEFAULT_DISCOVERY_PREFIX = "homeassistant"

# The order is the order of the setup screen, so a reader of either sees the
# same shape: identity, broker, topics, behaviour, diagnostics.
SETTING_NAMES = (
    "enabled",
    "host",
    "port",
    "tls",
    "ca_file",
    "username",
    "password",
    "node_id",
    "friendly_name",
    "base_topic",
    "ha_discovery_prefix",
    "ha_mode",
    "publish_keys",
    "screenshot",
    "screenshot_interval",
    "screenshot_delay",
    "osd_toast",
    "cam_telemetry",
    "oscam_telemetry",
    "oscam_port",
    "oscam_username",
    "oscam_password",
    "bouquets_for_select",
    "deep_standby_allowed",
    "wol_arm",
    "cec_standby_workaround",
    "softcam_restart_allowed",
    "softcam_autoheal",
    "softcam_autoheal_seconds",
    "epg_import_allowed",
    "uninstall_allowed",
    "log_level",
    "epg_grid_events",
)

# How a provisioning file's JSON is coerced onto each setting. Keyed explicitly
# rather than derived from the element's class, because the enigma2 config
# classes inherit from each other differently on different images.
SETTING_KINDS = {
    "enabled": "bool",
    "host": "text",
    "port": "int",
    "tls": "bool",
    "ca_file": "text",
    "username": "text",
    "password": "text",
    "node_id": "text",
    "friendly_name": "text",
    "base_topic": "text",
    "ha_discovery_prefix": "text",
    "ha_mode": "choice",
    "publish_keys": "bool",
    "screenshot": "choice",
    "screenshot_interval": "int",
    "screenshot_delay": "int",
    "osd_toast": "bool",
    "cam_telemetry": "bool",
    "oscam_telemetry": "bool",
    "oscam_port": "int",
    "oscam_username": "text",
    "oscam_password": "text",
    "bouquets_for_select": "text",
    "deep_standby_allowed": "bool",
    "wol_arm": "bool",
    "cec_standby_workaround": "bool",
    "softcam_restart_allowed": "bool",
    "softcam_autoheal": "bool",
    "softcam_autoheal_seconds": "int",
    "epg_import_allowed": "bool",
    "uninstall_allowed": "bool",
    "log_level": "choice",
    "epg_grid_events": "int",
}

CHOICES = {
    "ha_mode": HA_MODES,
    "screenshot": SCREENSHOT_MODES,
    "log_level": LOG_LEVELS,
}

SECRET_NAMES = ("password", "oscam_password")

# The range of every integer setting, for anything that validates one before it
# reaches the element - the OpenWebif page. `_build` states the same ranges on
# the elements themselves, and a test holds the two to each other, so a range
# changed in one place and not the other fails CI rather than the receiver.
INTEGER_LIMITS = {
    "port": (1, 65535),
    "screenshot_interval": SCREENSHOT_INTERVAL_LIMITS,
    "screenshot_delay": SCREENSHOT_DELAY_LIMITS,
    "oscam_port": (1, 65535),
    "softcam_autoheal_seconds": SOFTCAM_AUTOHEAL_LIMITS,
    "epg_grid_events": (0, 20),
}

# Characters, not bytes. A path and a bouquet filter are allowed to be long.
TEXT_LIMIT = 128
TEXT_LIMITS = {"ca_file": 1024, "bouquets_for_select": 1024}

# The settings whose change moves every retained topic somewhere else.
IDENTITY_SETTING_NAMES = ("node_id", "base_topic", "ha_discovery_prefix")

# What a topic segment may never hold: MQTT's two wildcards, and NUL.
_TOPIC_FORBIDDEN = ("+", "#", "\x00")
_NODE_ID = re.compile(r"[a-z0-9_]*")

_TRUE = ("1", "on", "true", "yes")
_FALSE = ("0", "off", "false", "no")


def _build():
    """Create the subsection once. Importing this module twice must not duplicate it."""
    plugins = getattr(config, "plugins", None)
    if plugins is None:
        config.plugins = ConfigSubsection()
        plugins = config.plugins

    existing = getattr(plugins, "mqttbridge", None)
    if existing is not None:
        return existing

    section = ConfigSubsection()
    section.enabled = ConfigYesNo(default=True)
    section.host = ConfigText(default="", fixed_size=False)
    section.port = ConfigInteger(default=1883, limits=(1, 65535))
    section.tls = ConfigYesNo(default=False)
    section.ca_file = ConfigText(default="", fixed_size=False)
    section.username = ConfigText(default="", fixed_size=False)
    section.password = ConfigPassword(default="", fixed_size=False)
    section.node_id = ConfigText(default="", fixed_size=False)
    section.friendly_name = ConfigText(default="", fixed_size=False)
    section.base_topic = ConfigText(default=DEFAULT_BASE_TOPIC, fixed_size=False)
    section.ha_discovery_prefix = ConfigText(default=DEFAULT_DISCOVERY_PREFIX, fixed_size=False)
    section.ha_mode = ConfigSelection(
        default="discovery", choices=[(mode, mode) for mode in HA_MODES]
    )
    section.publish_keys = ConfigYesNo(default=True)
    section.screenshot = ConfigSelection(
        default="on_zap",
        choices=[("off", _("off")), ("on_zap", _("on zap")), ("interval", _("at an interval"))],
    )
    section.screenshot_interval = ConfigInteger(default=60, limits=SCREENSHOT_INTERVAL_LIMITS)
    section.screenshot_delay = ConfigInteger(default=4, limits=SCREENSHOT_DELAY_LIMITS)
    # On by default, and never writable over MQTT: it is the kill-switch for a screen
    # that lives inside the GUI process, and a kill-switch reachable over the
    # broker is not one. In neither `cmd/config` list - it enables no command of
    # its own, and the `toast` capability already tells a consumer whether the
    # style is available.
    section.osd_toast = ConfigYesNo(default=True)
    section.cam_telemetry = ConfigYesNo(default=False)
    section.oscam_telemetry = ConfigYesNo(default=False)
    section.oscam_port = ConfigInteger(default=8888, limits=(1, 65535))
    section.oscam_username = ConfigText(default="", fixed_size=False)
    section.oscam_password = ConfigPassword(default="", fixed_size=False)
    # Internal only: never shown, provisioned or echoed. It makes neutral reader
    # handles stable without publishing an unsalted hash of a private label.
    section.oscam_identity_salt = ConfigText(default="", fixed_size=False)
    section.bouquets_for_select = ConfigText(default="", fixed_size=False)
    section.deep_standby_allowed = ConfigYesNo(default=False)
    # Off by default, and never writable over MQTT: it changes one of the
    # image's own settings, so it is asked for on the receiver. It switches the
    # image's Wake-on-LAN on where the image has one and does nothing where it
    # has not; switched off, it leaves the image's setting as it is (`wol.py`).
    # In neither `cmd/config` list - it enables no command, and `info.wol`
    # already says what the receiver's switch is, which is what a consumer
    # needs rather than what somebody asked for.
    section.wol_arm = ConfigYesNo(default=False)
    # Off by default, and never writable over MQTT: it is the kill-switch for
    # code that closes a screen somebody is looking at. Deliberately in neither
    # `cmd/config` list - it enables no command, and the `cec_workaround`
    # capability already tells a consumer whether it is at work.
    section.cec_standby_workaround = ConfigYesNo(default=False)
    # Permissions default off. This one gates a command that stops a running
    # program on the receiver, so it is granted on the receiver and never over
    # MQTT.
    section.softcam_restart_allowed = ConfigYesNo(default=False)
    section.softcam_autoheal = ConfigYesNo(default=False)
    section.softcam_autoheal_seconds = ConfigInteger(
        default=90, limits=SOFTCAM_AUTOHEAL_LIMITS
    )
    # Permissions default off. This one starts the image's EPG importer, whose
    # end freezes the picture's menus for seconds, so it is granted on the
    # receiver and never over MQTT.
    section.epg_import_allowed = ConfigYesNo(default=False)
    # Permissions default off, and this one most of all: `cmd/uninstall` takes
    # the plugin off the receiver, and after it has run nothing is left to take
    # a command that could undo it. Granted on the receiver, never over MQTT.
    section.uninstall_allowed = ConfigYesNo(default=False)
    section.log_level = ConfigSelection(
        default="info", choices=[(level, level) for level in LOG_LEVELS]
    )
    section.epg_grid_events = ConfigInteger(default=4, limits=(0, 20))

    plugins.mqttbridge = section
    return section


settings = _build()


def element(name, section=None):
    return getattr(section if section is not None else settings, name, None)


def value(name, section=None):
    found = element(name, section)
    return None if found is None else found.value


def save(section=None):
    """Persist every setting through enigma2's own settings file."""
    target = section if section is not None else settings
    for name in SETTING_NAMES:
        found = getattr(target, name, None)
        if found is not None:
            found.save()
    try:
        configfile.save()
    except Exception:
        LOG.exception("could not write enigma2's settings file")
        return False
    return True


# ------------------------------------------------------------------ coercion --


def _coerce_bool(raw):
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return bool(raw)
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise ValueError("expected true or false")


def _coerce_int(raw):
    if isinstance(raw, bool):
        raise ValueError("expected a number")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    raise ValueError("expected a number")


def _coerce_text(raw):
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(raw)
    raise ValueError("expected a string")


def _coerce_choice(name, raw):
    if not isinstance(raw, str):
        raise ValueError("expected a string")
    text = raw.strip().lower().replace(" ", "_").replace("-", "_")
    allowed = CHOICES.get(name, ())
    if text not in allowed:
        raise ValueError("expected one of " + ", ".join(allowed))
    return text


def coerce(name, raw):
    kind = SETTING_KINDS.get(name)
    if kind == "bool":
        return _coerce_bool(raw)
    if kind == "int":
        return _coerce_int(raw)
    if kind == "choice":
        return _coerce_choice(name, raw)
    return _coerce_text(raw)


def text_refusal(name, text):
    """Why `text` may not be stored in the text setting `name`, or None.

    🔴 enigma2 writes `/etc/enigma2/settings` as `key=value` lines with no
    escaping at all, and reads it back line by line. A newline inside a value is
    therefore a second settings line, under a name of the writer's choosing, the
    next time the receiver starts - `config.OpenWebif.auth=False`, or a
    permission. So every control character is refused, and the Unicode line and
    paragraph separators with them, since a line-splitting reader may honour
    those too. Refused, never stripped: a value that has been silently edited is
    not the value somebody typed.
    """
    limit = TEXT_LIMITS.get(name, TEXT_LIMIT)
    if len(text) > limit:
        return name + " is longer than " + str(limit) + " characters"
    for character in text:
        if unicodedata.category(character) in ("Cc", "Zl", "Zp"):
            return name + " may not contain control characters or line breaks"
    if name in IDENTITY_SETTING_NAMES and any(bad in text for bad in _TOPIC_FORBIDDEN):
        return name + " may not contain +, # or NUL"
    if name == "node_id" and not _NODE_ID.fullmatch(text):
        return "node_id may hold only a-z, 0-9 and _"
    return None


def validate_setting(name, raw):
    """One setting's new value, validated as every writer must: the value, or ValueError.

    Built from what this module already declares - `SETTING_KINDS`, `CHOICES`,
    `INTEGER_LIMITS` and the text rules - so there is one validator for every
    setting rather than one per form. The message names the setting.
    """
    if name not in SETTING_NAMES:
        raise ValueError(str(name) + " is not a setting")
    try:
        value = coerce(name, raw)
    except ValueError as error:
        raise ValueError(name + ": " + str(error)) from None
    kind = SETTING_KINDS.get(name)
    if kind == "int":
        minimum, maximum = INTEGER_LIMITS[name]
        if not minimum <= value <= maximum:
            raise ValueError(name + " must be between " + str(minimum) + " and " + str(maximum))
    elif kind == "text":
        refusal = text_refusal(name, value)
        if refusal:
            raise ValueError(refusal)
    return value


def save_settings(values, section=None):
    """Persist `values` - setting name to value - at once, restoring memory on failure.

    Each element is saved and enigma2's settings file is written once. When the
    write fails, every element named goes back to what it held, so a failed save
    leaves the running configuration as it was rather than half-applied.
    """
    target = section if section is not None else settings
    previous = {name: value(name, target) for name in values}
    try:
        for name, new in values.items():
            found = element(name, target)
            found.value = new
            found.save()
        configfile.save()
    except Exception:
        for name, old in previous.items():
            found = element(name, target)
            if found is None:
                continue
            found.value = old
            found.save()
        LOG.exception("could not write settings to enigma2's settings file")
        return False
    return True


def validate_remote_settings(raw, section=None):
    """Validate the complete, deliberately small remotely writable subset.

    `section` is the settings the result will be saved into, and it is where
    an omitted optional key's current value comes from. Reading the fallback
    from one section and writing the result into another would silently copy
    the module-global value over whatever the target actually held.
    """
    if not isinstance(raw, dict):
        raise ValueError("cmd/config takes a JSON object")
    unknown = sorted(set(raw) - set(REMOTE_SETTING_NAMES))
    required = {"publish_keys", "screenshot", "screenshot_interval"}
    missing = sorted(required - set(raw))
    if unknown:
        raise ValueError("the config object contains unknown settings")
    if missing:
        raise ValueError("missing setting(s): " + ", ".join(missing))
    publish_keys = raw["publish_keys"]
    screenshot = raw["screenshot"]
    interval = raw["screenshot_interval"]
    delay = raw.get("screenshot_delay", value("screenshot_delay", section))
    cam_telemetry = raw.get("cam_telemetry", value("cam_telemetry", section))
    oscam_telemetry = raw.get("oscam_telemetry", value("oscam_telemetry", section))
    autoheal = raw.get("softcam_autoheal", value("softcam_autoheal", section))
    autoheal_seconds = raw.get(
        "softcam_autoheal_seconds", value("softcam_autoheal_seconds", section)
    )
    if not isinstance(publish_keys, bool):
        raise ValueError("publish_keys must be true or false")
    if not isinstance(screenshot, str) or screenshot not in SCREENSHOT_MODES:
        raise ValueError("screenshot must be one of " + ", ".join(SCREENSHOT_MODES))
    if isinstance(interval, bool) or not isinstance(interval, int):
        raise ValueError("screenshot_interval must be an integer")
    minimum, maximum = SCREENSHOT_INTERVAL_LIMITS
    if not minimum <= interval <= maximum:
        raise ValueError(
            f"screenshot_interval must be between {minimum} and {maximum}"
        )
    if isinstance(delay, bool) or not isinstance(delay, int):
        raise ValueError("screenshot_delay must be an integer")
    minimum, maximum = SCREENSHOT_DELAY_LIMITS
    if not minimum <= delay <= maximum:
        raise ValueError(f"screenshot_delay must be between {minimum} and {maximum}")
    if not isinstance(cam_telemetry, bool):
        raise ValueError("cam_telemetry must be true or false")
    if not isinstance(oscam_telemetry, bool):
        raise ValueError("oscam_telemetry must be true or false")
    if not isinstance(autoheal, bool):
        raise ValueError("softcam_autoheal must be true or false")
    if isinstance(autoheal_seconds, bool) or not isinstance(autoheal_seconds, int):
        raise ValueError("softcam_autoheal_seconds must be an integer")
    minimum, maximum = SOFTCAM_AUTOHEAL_LIMITS
    if not minimum <= autoheal_seconds <= maximum:
        raise ValueError(
            f"softcam_autoheal_seconds must be between {minimum} and {maximum}"
        )
    return {
        "publish_keys": publish_keys,
        "screenshot": screenshot,
        "screenshot_interval": interval,
        "screenshot_delay": delay,
        "cam_telemetry": cam_telemetry,
        "oscam_telemetry": oscam_telemetry,
        "softcam_autoheal": autoheal,
        "softcam_autoheal_seconds": autoheal_seconds,
    }


def save_remote_settings(values, section=None):
    """Persist one validated remote replacement, restoring memory on failure."""
    return save_settings({name: values[name] for name in REMOTE_SETTING_NAMES}, section)


# -------------------------------------------------------------- provisioning --


def import_provisioning(path=None, section=None):
    """Apply `/etc/enigma2/mqttbridge.json`, then delete it.

    The file carries a broker password in clear, so it does not survive its own
    import. Two files do survive it, for the same reason: one that cannot be
    parsed, and one that imported nothing at all. Deleting either would destroy
    the only copy of what somebody meant to configure, and both are logged.

    Returns the list of setting names that were imported.
    """
    target_path = path or PROVISIONING_PATH
    target = section if section is not None else settings

    if not os.path.exists(target_path):
        return []

    try:
        with open(target_path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as error:
        LOG.error("%s could not be read (%s); leaving it in place", target_path, error)
        return []

    if not isinstance(raw, dict):
        LOG.error("%s is not a JSON object; leaving it in place", target_path)
        return []

    imported = []
    for key in sorted(raw):
        if key not in SETTING_NAMES:
            LOG.warning("%s: ignoring unknown key %s", target_path, key)
            continue
        found = getattr(target, key, None)
        if found is None:
            LOG.warning("%s: no such setting %s", target_path, key)
            continue
        try:
            found.value = coerce(key, raw[key])
        except ValueError as error:
            LOG.warning("%s: skipping %s (%s)", target_path, key, error)
            continue
        imported.append(key)

    if not imported:
        # Every key was unknown, or every value was rejected - a typo in the
        # file, not an instruction to configure nothing. Deleting it here would
        # throw away the only copy of what somebody meant to write, and they
        # would find an unchanged plugin and no file to correct.
        LOG.error(
            "%s imported nothing: no key in it is a setting this plugin has. "
            "Leaving it in place \u2014 correct the key names, or delete it.",
            target_path,
        )
        return []

    save(target)
    # Names only. One of these keys is the broker password.
    LOG.info("imported %d setting(s) from %s: %s",
             len(imported), target_path, ", ".join(imported))

    try:
        os.remove(target_path)
        LOG.info("removed %s (it held a password in clear)", target_path)
    except OSError as error:
        LOG.error("could not remove %s (%s) \u2014 it still holds a password", target_path, error)

    return imported
