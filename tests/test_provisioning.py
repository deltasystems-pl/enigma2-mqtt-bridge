"""The provisioning file: read once, applied, and deleted.

It holds a broker password in clear on the receiver's flash, so it does not
survive its own import. The exception is a file that could not be parsed:
deleting that would destroy the only copy of what somebody meant to configure.
"""

import json

from MQTTBridge import config as settings_module
from MQTTBridge import log as log_module


def write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_absent_file_is_a_no_op(tmp_path, settings):
    assert settings_module.import_provisioning(str(tmp_path / "nothing.json"), settings) == []


def test_values_are_applied_and_the_file_is_deleted(tmp_path, settings):
    target = tmp_path / "mqttbridge.json"
    write(target, {
        "host": "10.0.0.5",
        "port": 8883,
        "tls": True,
        "username": "enigma2box",
        "password": "hunter2",
        "friendly_name": "Living room receiver",
        "ha_mode": "integration",
        "log_level": "debug",
        "epg_grid_events": 0,
    })

    imported = settings_module.import_provisioning(str(target), settings)

    assert sorted(imported) == [
        "epg_grid_events", "friendly_name", "ha_mode", "host", "log_level",
        "password", "port", "tls", "username",
    ]
    assert settings.host.value == "10.0.0.5"
    assert settings.port.value == 8883
    assert settings.tls.value is True
    assert settings.password.value == "hunter2"
    assert settings.ha_mode.value == "integration"
    assert settings.epg_grid_events.value == 0
    assert not target.exists()


def test_values_are_saved_through_enigma2(tmp_path, settings):
    from Components.config import configfile

    target = tmp_path / "mqttbridge.json"
    write(target, {"host": "10.0.0.5"})
    settings_module.import_provisioning(str(target), settings)

    assert settings.host.saved_value == "10.0.0.5"
    assert configfile.save_calls == 1


def test_a_file_that_imported_nothing_is_kept(tmp_path, settings, plugin_log):
    """Every key misspelt is a typo, not an instruction to configure nothing.

    Deleting it would leave somebody with an unchanged plugin and no file to
    correct — and the file is the only copy of what they meant to write.
    """
    target = tmp_path / "mqttbridge.json"
    write(target, {"mqtt_host": "10.0.0.5", "broker_port": 1883})

    assert settings_module.import_provisioning(str(target), settings) == []

    assert target.exists()
    assert settings.host.value == ""
    written = plugin_log()
    assert "imported nothing" in written
    assert "mqtt_host" in written


def test_a_file_whose_every_value_was_rejected_is_kept(tmp_path, settings):
    target = tmp_path / "mqttbridge.json"
    write(target, {"port": "eighteen-eighty-three", "ha_mode": "whatever"})

    assert settings_module.import_provisioning(str(target), settings) == []
    assert target.exists()


def test_one_good_key_is_enough_for_the_file_to_go(tmp_path, settings):
    target = tmp_path / "mqttbridge.json"
    write(target, {"host": "10.0.0.5", "nonsense": True})

    assert settings_module.import_provisioning(str(target), settings) == ["host"]
    assert not target.exists()


def test_unknown_keys_are_ignored_and_the_rest_still_applies(tmp_path, settings, plugin_log):
    target = tmp_path / "mqttbridge.json"
    write(target, {"host": "10.0.0.5", "mqtt_host": "wrong", "colour": "blue"})

    imported = settings_module.import_provisioning(str(target), settings)

    assert imported == ["host"]
    assert settings.host.value == "10.0.0.5"
    written = plugin_log()
    assert "mqtt_host" in written
    assert "colour" in written
    assert not target.exists()


def test_a_value_of_the_wrong_type_is_skipped(tmp_path, settings, plugin_log):
    target = tmp_path / "mqttbridge.json"
    write(target, {"host": "10.0.0.5", "port": "eighteen-eighty-three", "publish_keys": "maybe"})

    imported = settings_module.import_provisioning(str(target), settings)

    assert imported == ["host"]
    assert settings.port.value == 1883
    assert settings.publish_keys.value is True
    assert "skipping port" in plugin_log()


def test_an_unknown_choice_is_skipped(tmp_path, settings):
    target = tmp_path / "mqttbridge.json"
    write(target, {"ha_mode": "whatever"})
    assert settings_module.import_provisioning(str(target), settings) == []
    assert settings.ha_mode.value == "discovery"
    assert target.exists()


def test_friendly_spellings_of_yes_and_no_are_accepted(tmp_path, settings):
    target = tmp_path / "mqttbridge.json"
    write(target, {"tls": "yes", "deep_standby_allowed": "off", "enabled": 0})
    settings_module.import_provisioning(str(target), settings)
    assert settings.tls.value is True
    assert settings.deep_standby_allowed.value is False
    assert settings.enabled.value is False


def test_the_documented_screenshot_spelling_is_accepted(tmp_path, settings):
    """SETUP.md shows `"screenshot": "on zap"`; the config value is `on_zap`."""
    target = tmp_path / "mqttbridge.json"
    write(target, {"screenshot": "on zap"})
    assert settings_module.import_provisioning(str(target), settings) == ["screenshot"]
    assert settings.screenshot.value == "on_zap"


def test_a_malformed_file_is_left_where_it_is(tmp_path, settings, plugin_log):
    target = tmp_path / "mqttbridge.json"
    target.write_text('{"host": "10.0.0.5"', encoding="utf-8")

    assert settings_module.import_provisioning(str(target), settings) == []

    assert target.exists()
    assert settings.host.value == ""
    assert "could not be read" in plugin_log()


def test_a_json_array_is_not_a_provisioning_file(tmp_path, settings):
    target = tmp_path / "mqttbridge.json"
    target.write_text('["host", "10.0.0.5"]', encoding="utf-8")
    assert settings_module.import_provisioning(str(target), settings) == []
    assert target.exists()


def test_the_log_names_the_keys_and_never_the_values(tmp_path, settings, plugin_log):
    log_module.register_secret("hunter2")
    target = tmp_path / "mqttbridge.json"
    write(target, {"host": "10.0.0.5", "password": "hunter2"})

    settings_module.import_provisioning(str(target), settings)

    written = plugin_log()
    assert "password" in written
    assert "hunter2" not in written
