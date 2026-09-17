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
    "cam_telemetry": False,
    "bouquets_for_select": "",
    "deep_standby_allowed": False,
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
    }


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
])
def test_remote_settings_reject_partial_unknown_or_invalid_values(payload):
    with pytest.raises(ValueError):
        settings_module.validate_remote_settings(payload)


def test_save_writes_enigma2s_settings_file():
    from Components.config import configfile

    settings_module.settings.host.value = "10.0.0.5"
    assert settings_module.save() is True
    assert settings_module.settings.host.saved_value == "10.0.0.5"
    assert configfile.save_calls == 1
