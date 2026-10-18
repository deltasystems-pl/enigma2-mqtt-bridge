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
from .log import get_logger, redact

LOG = get_logger("config")

PROVISIONING_PATH = "/etc/enigma2/mqttbridge.json"

HA_MODES = ("discovery", "integration", "off")
LOG_LEVELS = ("error", "warning", "info", "debug")
SCREENSHOT_MODES = ("off", "on_zap", "interval")
# The keys that may open the receiver menu (`hotkey.py`), and `off`.
MENU_HOTKEYS = ("help_long", "red_long", "f3", "f4", "off")
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
    # Not a permission either: which key opens a menu the receiver already allows.
    "menu_hotkey",
    # Nor this: how see-through the menu's background is.
    "menu_transparency",
)
# Published in `info.settings` and refused by `cmd/config`, like any other key
# that is not in the allowlist above. A setting that *enables* a command is
# never writable over MQTT: it is set on the receiver - its setup screen, the
# provisioning file, or the OpenWebif page, which is exactly as open as the
# receiver's web interface (ADR-0009). Echoing it lets a consumer hide a control
# the box would always refuse instead of offering one that fails.
# `update_check` is not a permission, and is on this side of the line for the same
# reason: it is what lets the receiver reach the internet at all, which is the
# receiver's to decide and not the broker's.
READ_ONLY_SETTING_NAMES = (
    "deep_standby_allowed", "softcam_restart_allowed", "epg_import_allowed",
    "uninstall_allowed", "update_check", "update_allowed", "menu_allowed",
)
# A refusal names the keys it refused, and they are the sender's words: this many
# of them, each cut to this length, so the sentence on `last_error` stays short.
UNKNOWN_SETTINGS_NAMED = 5
UNKNOWN_SETTING_NAME_LIMIT = 32
SCREENSHOT_INTERVAL_LIMITS = (5, 3600)
SCREENSHOT_DELAY_LIMITS = (1, 30)
# Below thirty seconds the detector would be reading noise: a healthy encrypted
# channel refreshes its ECM file about every ten, and the spread is nine to
# eleven. Above ten minutes it is no longer a repair.
SOFTCAM_AUTOHEAL_LIMITS = (30, 600)
# How see-through the receiver menu's background is, in percent: 0 is opaque. Above 80 the text
# would stand on the picture alone. A step of 10, so LEFT and RIGHT on the television move it by
# one step each and every writer offers the same nine values.
MENU_TRANSPARENCY_LIMITS = (0, 80)
MENU_TRANSPARENCY_STEP = 10
MENU_TRANSPARENCY_DEFAULT = 20
MENU_TRANSPARENCY_CHOICES = tuple(
    range(MENU_TRANSPARENCY_LIMITS[0], MENU_TRANSPARENCY_LIMITS[1] + 1, MENU_TRANSPARENCY_STEP)
)
# The settings that belong to the receiver menu: the television's "Menu" section and the
# OpenWebif page's Menu subpage show these, and only these.
MENU_SETTING_NAMES = ("menu_allowed", "menu_hotkey", "menu_transparency")

DEFAULT_BASE_TOPIC = "enigma2"
DEFAULT_DISCOVERY_PREFIX = "homeassistant"

# The order is the order of the setup screen, so a reader of either sees the
# same shape: identity, broker, topics, behaviour, diagnostics - and the receiver
# menu last, under a heading of its own on the screen.
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
    "update_check",
    "update_allowed",
    "log_level",
    "epg_grid_events",
    "menu_allowed",
    "menu_hotkey",
    "menu_transparency",
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
    "update_check": "bool",
    "update_allowed": "bool",
    "menu_allowed": "bool",
    "menu_hotkey": "choice",
    "menu_transparency": "int",
    "log_level": "choice",
    "epg_grid_events": "int",
}

CHOICES = {
    "ha_mode": HA_MODES,
    "screenshot": SCREENSHOT_MODES,
    "menu_hotkey": MENU_HOTKEYS,
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
    "menu_transparency": MENU_TRANSPARENCY_LIMITS,
}
# Integer settings that take only every n-th value of their range. A value between two steps is
# refused with a sentence that says so, never rounded: a rounded value is not the one sent.
INTEGER_STEPS = {"menu_transparency": MENU_TRANSPARENCY_STEP}

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
    # Off by default, and never writable over MQTT: with it on the receiver asks the
    # plugin's release origin, on the internet, once a day and when `cmd/update_check`
    # asks. A receiver never looks for the internet on its own, so this is asked for on
    # the receiver (`updatecheck.py`).
    section.update_check = ConfigYesNo(default=False)
    # Permissions default off. `cmd/update` installs a signed release and restarts the
    # interface to run it (`selfupdate.py`): the one command that replaces the code taking
    # the commands, so it is granted on the receiver and never over MQTT. The television
    # and the OpenWebif page need it for nothing - whoever stands there could set it.
    section.update_allowed = ConfigYesNo(default=False)
    # On by default (ADR-0020 decision 8), and never writable over MQTT: the receiver menu shows
    # what Home Assistant sends and sends back only a row's id and a value (`menu.py`), so what
    # it can do is what the user put into the menu in Home Assistant. Off, there is no menu, no
    # Extensions entry, no hotkey and no subscription to the menu topic.
    section.menu_allowed = ConfigYesNo(default=True)
    section.menu_hotkey = ConfigSelection(
        default="help_long",
        choices=[
            ("help_long", _("HELP, held")),
            ("red_long", _("RED, held")),
            ("f3", "F3"),
            ("f4", "F4"),
            ("off", _("off")),
        ],
    )
    # A selection, not a ConfigInteger: on the television LEFT and RIGHT step a selection by one
    # choice - 10 % - where an integer field moves a cursor over its digits. The keys are ints,
    # so the value read back is the number `info.settings` publishes (`transparency`).
    section.menu_transparency = ConfigSelection(
        default=MENU_TRANSPARENCY_DEFAULT,
        choices=[(percent, f"{percent} %") for percent in MENU_TRANSPARENCY_CHOICES],
    )
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
    if name == "menu_transparency":
        return transparency(None if found is None else found.value)
    return None if found is None else found.value


def transparency(raw):
    """`menu_transparency` as the int it is meant to be, whatever the element holds.

    The element's keys are ints and enigma2's selection hands back the key it matched, so this
    is the identity on every image measured. It is here for the one that would hand back the
    text from its settings file: an int, one of the choices, or the default.
    """
    if isinstance(raw, str) and raw.strip().isdigit():
        raw = int(raw.strip())
    if isinstance(raw, bool) or not isinstance(raw, int):
        return MENU_TRANSPARENCY_DEFAULT
    return raw if raw in MENU_TRANSPARENCY_CHOICES else MENU_TRANSPARENCY_DEFAULT


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


def step_refusal(name, number):
    """Why `number` is not one of the integer setting `name`'s steps, or None."""
    step = INTEGER_STEPS.get(name)
    if step is None:
        return None
    if (number - INTEGER_LIMITS[name][0]) % step:
        return name + " must be a multiple of " + str(step)
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
        refusal = step_refusal(name, value)
        if refusal:
            raise ValueError(refusal)
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


def _named(name):
    """A key somebody sent, as it may be shown: in quotes, printable, of bounded length.

    Redacted whole, before it is cut: a secret sent as a name and cut in the
    middle would no longer be the registered text, and its head would be
    shown. The quotes are the sentence's own, so an apostrophe in a name is
    shown as `?`, like a character that cannot be printed - a name cannot
    close its quotes and pass for two names, or for the count that follows.
    """
    text = "".join(
        character if character.isprintable() and character != "'" else "?"
        for character in redact(str(name))
    )
    if len(text) > UNKNOWN_SETTING_NAME_LIMIT:
        text = text[:UNKNOWN_SETTING_NAME_LIMIT] + "\u2026"
    return "'" + text + "'"


def unknown_settings_refusal(unknown):
    """The sentence that refuses `unknown`, the sorted keys outside the allowlist.

    It names them, because "unknown settings" alone left the reader to compare
    the payload with the list by hand. Only the names: a value is never echoed.
    """
    sentence = "the config object contains unknown settings: " + ", ".join(
        _named(name) for name in unknown[:UNKNOWN_SETTINGS_NAMED]
    )
    if len(unknown) > UNKNOWN_SETTINGS_NAMED:
        sentence += " and " + str(len(unknown) - UNKNOWN_SETTINGS_NAMED) + " more"
    return sentence


def _remote_bool(name, raw):
    if not isinstance(raw, bool):
        raise ValueError(name + " must be true or false")
    return raw


def _remote_int(name, raw):
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(name + " must be an integer")
    minimum, maximum = INTEGER_LIMITS[name]
    if not minimum <= raw <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    refusal = step_refusal(name, raw)
    if refusal:
        raise ValueError(refusal)
    return raw


def _remote_choice(name, raw):
    allowed = CHOICES[name]
    if not isinstance(raw, str) or raw not in allowed:
        raise ValueError(name + " must be one of " + ", ".join(allowed))
    return raw


def validate_remote_settings(raw, section=None):
    """Validate a `cmd/config` object: any part of the remotely writable subset.

    Every key is optional: one left out keeps its current value, read from `section` - the
    settings the result will be saved into. Reading the fallback from one section and writing
    the result into another would silently copy the module-global value over whatever the
    target actually held. An empty object is valid and changes nothing. A key outside the
    subset, or a value of the wrong type or out of its range, refuses the whole object.

    The result is always the whole subset, what was sent and what was kept, so that
    `save_remote_settings` writes it at once.
    """
    if not isinstance(raw, dict):
        raise ValueError("cmd/config takes a JSON object")
    unknown = sorted(set(raw) - set(REMOTE_SETTING_NAMES))
    if unknown:
        raise ValueError(unknown_settings_refusal(unknown))
    values = {}
    for name in REMOTE_SETTING_NAMES:
        if name not in raw:
            values[name] = value(name, section)
            continue
        kind = SETTING_KINDS[name]
        if kind == "bool":
            values[name] = _remote_bool(name, raw[name])
        elif kind == "int":
            values[name] = _remote_int(name, raw[name])
        else:
            values[name] = _remote_choice(name, raw[name])
    return values


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
