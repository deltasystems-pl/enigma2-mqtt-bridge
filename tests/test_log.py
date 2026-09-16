"""The log, and the promise that the broker password is never in it.

TROUBLESHOOTING.md tells a user that finding the password in this file is a bug
worth a security report. That promise is only worth something if something
checks it, so the test below writes a password into the configuration, runs a
whole session through the plugin at `debug` — connect, publish, a command, an
error — and then reads the file back.
"""

import logging

from MQTTBridge import log as log_module

NODE = "vuuno4kse_005301"
PASSWORD = "correct-horse-battery-staple"


def test_the_password_never_reaches_the_log(make_bridge, factory, settings, isolated_log):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.username.value = "enigma2box"
    settings.password.value = PASSWORD
    settings.log_level.value = "debug"

    bridge = make_bridge()
    bridge.start()
    factory.client.fire_connect()
    # An error path as well: a refused command, and a handler that raises.
    factory.client.fire_message("enigma2/" + NODE + "/cmd/ha_mode", PASSWORD.encode("utf-8"))
    factory.client.fire_message("enigma2/" + NODE + "/cmd/nonsense", PASSWORD.encode("utf-8"))
    log_module.get_logger("test").error("a line mentioning %s directly", PASSWORD)
    bridge.stop()

    written = isolated_log.read_text(encoding="utf-8")
    assert written, "the plugin wrote nothing at all"
    assert PASSWORD not in written
    assert "***" in written
    # And it did log the session, so the absence above is not an empty file.
    assert "connected to 10.0.0.5" in written
    assert "subscribed to" in written


def test_redact_replaces_every_occurrence():
    log_module.register_secret("s3cret")
    assert log_module.redact("s3cret and s3cret") == "*** and ***"
    assert log_module.redact("nothing here") == "nothing here"


def test_redact_survives_a_non_string():
    log_module.register_secret("s3cret")
    assert log_module.redact(1883) == "1883"


def test_an_empty_password_is_not_registered():
    log_module.register_secret("")
    log_module.register_secret("   ")
    log_module.register_secret(None)
    assert log_module.redact("anything") == "anything"


def test_the_handler_is_capped_and_rotated(isolated_log):
    log_module.configure("info", str(isolated_log))
    handler = logging.getLogger(log_module.LOGGER_NAME).handlers[0]
    assert handler.maxBytes == 1000000
    assert handler.backupCount == 2


def test_configure_twice_keeps_the_same_handler(isolated_log):
    log_module.configure("info", str(isolated_log))
    first = logging.getLogger(log_module.LOGGER_NAME).handlers[0]
    log_module.configure("debug", str(isolated_log))
    logger = logging.getLogger(log_module.LOGGER_NAME)
    assert logger.handlers[0] is first
    assert logger.level == logging.DEBUG


def test_an_unwritable_primary_path_falls_back(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback.log"
    monkeypatch.setattr(log_module, "PRIMARY_LOG_PATH", "/nowhere/that/exists/mqttbridge.log")
    monkeypatch.setattr(log_module, "FALLBACK_LOG_PATH", str(fallback))

    assert log_module.configure("info") == str(fallback)
    written = fallback.read_text(encoding="utf-8")
    assert "/nowhere/that/exists/mqttbridge.log" in written
    assert "logging to" in written


def test_no_writable_path_at_all_is_survivable(monkeypatch):
    monkeypatch.setattr(log_module, "PRIMARY_LOG_PATH", "/nowhere/a/mqttbridge.log")
    monkeypatch.setattr(log_module, "FALLBACK_LOG_PATH", "/nowhere/b/mqttbridge.log")

    assert log_module.configure("info") is None
    # And logging still does not raise.
    log_module.get_logger("test").error("no file anywhere")


def test_an_unknown_level_means_info():
    assert log_module.level_value("shouty") == logging.INFO
    assert log_module.level_value(None) == logging.INFO
    assert log_module.level_value("DEBUG") == logging.DEBUG


def test_the_log_level_setting_reaches_the_logger(make_bridge, settings):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.log_level.value = "error"
    make_bridge().start()
    assert logging.getLogger(log_module.LOGGER_NAME).level == logging.ERROR
