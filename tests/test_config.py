"""Every setting the contract names exists, with the default the contract names.

A default that drifts is not a bug anybody notices until a box is provisioned
from a file that omits the key, which is exactly when nobody is watching.
"""

import pytest

from MQTTBridge import config as settings_module

EXPECTED_DEFAULTS = {
    "enabled": True,
    "host": "",
    "port": 1883,
    "tls": False,
    "ca_file": "",
    "username": "",
    "password": "",
    "node_id": "",
    "friendly_name": "",
    "base_topic": "enigma2",
    "ha_discovery_prefix": "homeassistant",
    "ha_mode": "discovery",
    "publish_keys": True,
    "screenshot": "on_zap",
    "screenshot_interval": 60,
    "screenshot_delay": 4,
    "osd_toast": True,
    "cam_telemetry": False,
    "oscam_telemetry": False,
    "oscam_port": 8888,
    "oscam_username": "",
    "oscam_password": "",
    "bouquets_for_select": "",
    "deep_standby_allowed": False,
    "wol_arm": False,
    "cec_standby_workaround": False,
    "softcam_restart_allowed": False,
    "softcam_autoheal": False,
    "softcam_autoheal_seconds": 90,
    "epg_import_allowed": False,
    "uninstall_allowed": False,
    "update_check": False,
    "update_allowed": False,
    "log_level": "info",
    "epg_grid_events": 4,
}


def test_every_documented_setting_exists():
    missing = [name for name in EXPECTED_DEFAULTS if not hasattr(settings_module.settings, name)]
    assert not missing


def test_setting_names_match_the_elements():
    assert set(settings_module.SETTING_NAMES) == set(EXPECTED_DEFAULTS)
    assert len(settings_module.SETTING_NAMES) == len(set(settings_module.SETTING_NAMES))


@pytest.mark.parametrize("name", sorted(EXPECTED_DEFAULTS))
def test_default(name):
    element = getattr(settings_module.settings, name)
    assert element.value == EXPECTED_DEFAULTS[name]


def test_the_section_is_built_once():
    from Components.config import config

    assert settings_module.settings is config.plugins.mqttbridge
    assert settings_module._build() is settings_module.settings


def test_port_is_bounded():
    assert settings_module.settings.port.limits == (1, 65535)


def test_every_setting_has_a_coercion_kind():
    assert set(settings_module.SETTING_KINDS) == set(settings_module.SETTING_NAMES)


def test_the_choice_settings_agree_with_their_elements():
    for name, allowed in settings_module.CHOICES.items():
        element = getattr(settings_module.settings, name)
        assert [key for key, _label in element.choices] == list(allowed)


def test_ha_modes_are_the_contract_three():
    assert settings_module.HA_MODES == ("discovery", "integration", "off")


def test_the_password_is_a_password_field():
    from Components.config import ConfigPassword

    assert isinstance(settings_module.settings.password, ConfigPassword)
    assert isinstance(settings_module.settings.oscam_password, ConfigPassword)


def test_remote_settings_are_exact_and_strictly_typed():
    assert settings_module.validate_remote_settings({
        "publish_keys": False,
        "screenshot": "interval",
        "screenshot_interval": 300,
    }) == {
        "publish_keys": False,
        "screenshot": "interval",
        "screenshot_interval": 300,
        "screenshot_delay": 4,
        "cam_telemetry": False,
        "oscam_telemetry": False,
        "softcam_autoheal": False,
        "softcam_autoheal_seconds": 90,
    }


def test_an_omitted_key_falls_back_to_the_section_that_will_be_saved():
    """🔴 Reading the fallback from one section and writing into another copies
    the module-global value over whatever the target actually held."""
    from types import SimpleNamespace

    class _Element:
        def __init__(self, value):
            self.value = value

    settings_module.settings.screenshot_delay.value = 4
    settings_module.settings.cam_telemetry.value = False
    other = SimpleNamespace(
        screenshot_delay=_Element(19),
        cam_telemetry=_Element(True),
        oscam_telemetry=_Element(True),
        softcam_autoheal=_Element(True),
        softcam_autoheal_seconds=_Element(240),
    )

    values = settings_module.validate_remote_settings(
        {"publish_keys": True, "screenshot": "off", "screenshot_interval": 60}, other
    )

    assert values["screenshot_delay"] == 19
    assert values["cam_telemetry"] is True
    assert values["oscam_telemetry"] is True
    assert values["softcam_autoheal"] is True
    assert values["softcam_autoheal_seconds"] == 240


@pytest.mark.parametrize("payload", [
    {},
    {"publish_keys": True, "screenshot": "off", "screenshot_interval": 60, "host": "x"},
    {"publish_keys": 1, "screenshot": "off", "screenshot_interval": 60},
    {"publish_keys": True, "screenshot": "sometimes", "screenshot_interval": 60},
    {"publish_keys": True, "screenshot": "off", "screenshot_interval": True},
    {"publish_keys": True, "screenshot": "off", "screenshot_interval": 4},
    {"publish_keys": True, "screenshot": "off", "screenshot_interval": 3601},
    {"publish_keys": True, "screenshot": "off", "screenshot_interval": 60,
     "screenshot_delay": True},
    {"publish_keys": True, "screenshot": "off", "screenshot_interval": 60,
     "screenshot_delay": 0},
    {"publish_keys": True, "screenshot": "off", "screenshot_interval": 60,
     "screenshot_delay": 31},
    {"publish_keys": True, "screenshot": "off", "screenshot_interval": 60,
     "cam_telemetry": 1},
    {"publish_keys": True, "screenshot": "off", "screenshot_interval": 60,
     "oscam_telemetry": 1},
])
def test_remote_settings_reject_partial_unknown_or_invalid_values(payload):
    with pytest.raises(ValueError):
        settings_module.validate_remote_settings(payload)


def test_an_unknown_remote_setting_is_named_in_the_refusal():
    with pytest.raises(ValueError) as refused:
        settings_module.validate_remote_settings({
            "publish_keys": True, "screenshot": "off", "screenshot_interval": 60,
            "update_allowed": True,
        })
    assert str(refused.value) == (
        "the config object contains unknown settings: update_allowed"
    )


def test_several_unknown_remote_settings_are_all_named_in_order():
    with pytest.raises(ValueError) as refused:
        settings_module.validate_remote_settings({
            "wol_arm": True, "publish_keys": True, "deep_standby_allowed": True,
            "screenshot": "off", "screenshot_interval": 60, "host": "192.0.2.1",
        })
    assert str(refused.value) == (
        "the config object contains unknown settings: deep_standby_allowed, host, wol_arm"
    )


def test_the_refusal_names_a_bounded_number_of_unknown_settings():
    payload = {"publish_keys": True, "screenshot": "off", "screenshot_interval": 60}
    payload.update({f"key_{index:02d}": 1 for index in range(40)})
    with pytest.raises(ValueError) as refused:
        settings_module.validate_remote_settings(payload)
    named = settings_module.UNKNOWN_SETTINGS_NAMED
    assert str(refused.value) == (
        "the config object contains unknown settings: "
        + ", ".join(f"key_{index:02d}" for index in range(named))
        + " and " + str(40 - named) + " more"
    )


def test_an_unknown_setting_name_is_cut_and_made_printable():
    """The name comes from whoever published the command, so it is not trusted as text."""
    limit = settings_module.UNKNOWN_SETTING_NAME_LIMIT
    with pytest.raises(ValueError) as refused:
        settings_module.validate_remote_settings({
            "publish_keys": True, "screenshot": "off", "screenshot_interval": 60,
            "a\nb\x00c\u2028d": 1, "x" * (limit + 50): 1,
        })
    assert str(refused.value) == (
        "the config object contains unknown settings: a?b?c?d, " + "x" * limit + "\u2026"
    )


def test_a_secret_sent_as_a_setting_name_is_not_echoed():
    from MQTTBridge import log as log_module

    log_module.register_secret("correct-horse")
    with pytest.raises(ValueError) as refused:
        settings_module.validate_remote_settings({
            "publish_keys": True, "screenshot": "off", "screenshot_interval": 60,
            "correct-horse": 1,
        })
    assert str(refused.value) == "the config object contains unknown settings: ***"


def test_save_writes_enigma2s_settings_file():
    from Components.config import configfile

    settings_module.settings.host.value = "10.0.0.5"
    assert settings_module.save() is True
    assert settings_module.settings.host.saved_value == "10.0.0.5"
    assert configfile.save_calls == 1
