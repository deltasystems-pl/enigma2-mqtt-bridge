"""A save on the receiver reconnects only for the connection.

The setup screen's Save and the OpenWebif page's settings forms used to end the MQTT session and
build a new one for every change - seconds with the menu greyed and Home Assistant watching a
reconnect, for a transparency or a hotkey. Now only the settings that make the session - the
broker, the login, TLS, the node id and the base topic - and the plugin's own switch rebuild it;
every other setting is applied under the open session, as `cmd/config` applies its own, and the
same client publishes `info`, the announcement and the discovery payloads again.
"""

import json

import pytest
from conftest import MainLoop, plugin_component_module
from test_menu import CONTEXT, NODE, STATUS_TOPIC, World

from MQTTBridge import bridge as bridge_module
from MQTTBridge import config as settings_module
from MQTTBridge import plugin as plugin_module
from MQTTBridge import setup as setup_screen

ROOT = "enigma2/" + NODE
INFO = ROOT + "/info"
AVAILABILITY = ROOT + "/availability"
MENU_TOPIC = "enigma2mqtt/menu/" + NODE
DEVICE_TOPIC = "homeassistant/device/" + NODE + "/config"
ANNOUNCEMENT = "enigma2mqtt/discovery/" + NODE + "/config"


class FakeSession:
    def __init__(self):
        self.opened = []

    def open(self, what, *args, **kwargs):
        self.opened.append(what)

    def openWithCallback(self, callback, what, *args, **kwargs):
        self.opened.append(what)


def save_on_the_screen(bridge, **values):
    """What a household does: change fields on the setup screen, then the green key."""
    screen = setup_screen.MQTTBridgeSetup(FakeSession(), settings=bridge.settings, bridge=bridge)
    for name, value in values.items():
        getattr(bridge.settings, name).value = value
    screen.keySave()
    return screen


def other_value(name, current):
    """A valid value for `name` that is not `current`."""
    kind = settings_module.SETTING_KINDS[name]
    if kind == "bool":
        return not current
    if kind == "choice":
        return next(choice for choice in settings_module.CHOICES[name] if choice != current)
    if name == "menu_transparency":
        return 60 if current != 60 else 70
    if kind == "int":
        low, high = settings_module.INTEGER_LIMITS[name]
        return low if current != low else high
    return {
        "friendly_name": "Kitchen receiver",
        "ha_discovery_prefix": "ha",
        "oscam_username": "reader",
        "oscam_password": "a-new-oscam-secret",
        "bouquets_for_select": "Favourites",
    }[name]


RECONNECTING = ("enabled",) + bridge_module.CONNECTION_SETTING_NAMES
LIVE = tuple(name for name in settings_module.SETTING_NAMES if name not in RECONNECTING)


def test_the_settings_that_reconnect_are_the_connection_and_the_switch():
    assert set(RECONNECTING) == {
        "enabled", "host", "port", "tls", "ca_file", "username", "password", "node_id",
        "base_topic",
    }
    # Every setting is on one side or the other, and a new setting lands on the live side
    # unless somebody adds it to the connection on purpose.
    assert set(LIVE) | set(RECONNECTING) == set(settings_module.SETTING_NAMES)


@pytest.mark.parametrize("name", LIVE)
def test_a_save_of_any_other_setting_keeps_the_session(live_bridge, factory, name):
    client = factory.client
    clients = len(factory.clients)
    current = settings_module.value(name, live_bridge.settings)
    client.clear()

    save_on_the_screen(live_bridge, **{name: other_value(name, current)})

    assert len(factory.clients) == clients, "the save reconnected"
    assert factory.client is client
    assert client.disconnect_calls == 0
    assert live_bridge.last_save_reconnected is False
    assert [entry for entry in client.all_for(AVAILABILITY) if entry.text == "offline"] == []
    # The same client says it again: `info` with every setting, capability and the menu.
    assert client.last(INFO) is not None
    published = client.last(INFO).json()["settings"]
    if name in published:
        assert published[name] == settings_module.value(name, live_bridge.settings)


@pytest.mark.parametrize("name, value", [
    ("host", "10.0.0.6"), ("port", 1884), ("tls", True), ("ca_file", "/etc/ca.pem"),
    ("username", "receiver"), ("password", "a-new-secret"),
])
def test_a_save_that_changes_the_connection_reconnects_and_says_offline_first(
    live_bridge, factory, name, value
):
    old = factory.client
    old.clear()

    save_on_the_screen(live_bridge, **{name: value})

    assert len(factory.clients) == 2
    assert live_bridge.last_save_reconnected is True
    assert old.client_disconnected()
    assert old.last(AVAILABILITY).text == "offline"


@pytest.mark.parametrize("name, value", [("node_id", "vuuno4kse_005302"),
                                         ("base_topic", "receivers")])
def test_a_rename_reconnects_and_says_no_offline_under_either_name(live_bridge, factory, name,
                                                                    value):
    old = factory.client
    old.clear()

    save_on_the_screen(live_bridge, **{name: value})

    assert len(factory.clients) == 2
    assert [entry for entry in old.published if entry.text == "offline"] == []


def test_switching_the_plugin_off_reconnects_into_idle_with_offline_retained(live_bridge,
                                                                             factory):
    old = factory.client
    save_on_the_screen(live_bridge, enabled=False)
    assert old.last(AVAILABILITY).text == "offline"
    assert live_bridge.client is None
    assert live_bridge.idle_reason == "the plugin is switched off in its settings"


def test_switching_it_on_again_starts_a_session(live_bridge, factory):
    save_on_the_screen(live_bridge, enabled=False)
    save_on_the_screen(live_bridge, enabled=True)
    assert live_bridge.running
    assert len(factory.clients) == 2


def test_an_idle_bridge_starts_on_any_save(make_bridge, factory, settings):
    bridge = make_bridge()
    bridge.start()
    assert not bridge.running and factory.client is None
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    save_on_the_screen(bridge, screenshot_delay=9)
    assert bridge.running and len(factory.clients) == 1
    assert bridge.last_save_reconnected is True


def test_a_stopped_bridge_starts_again_on_a_save(live_bridge, factory):
    """`stop` drops the client and keeps what the session was built from: still no session."""
    live_bridge.stop()
    assert live_bridge.client is None
    save_on_the_screen(live_bridge, menu_transparency=60)
    assert live_bridge.running and len(factory.clients) == 2
    assert live_bridge.last_save_reconnected is True


def test_a_save_that_changed_nothing_publishes_nothing(live_bridge, factory, plugin_log):
    client = factory.client
    client.clear()
    save_on_the_screen(live_bridge)
    assert client.published == []
    assert len(factory.clients) == 1
    assert "nothing the bridge runs with has changed" in plugin_log()


def test_a_connection_change_and_another_together_reconnect_once(live_bridge, factory):
    save_on_the_screen(live_bridge, host="10.0.0.6", menu_transparency=60)
    assert len(factory.clients) == 2
    factory.client.fire_connect()
    assert factory.client.last(INFO).json()["settings"]["menu_transparency"] == 60


def test_the_log_names_what_was_applied_and_never_a_value(live_bridge, plugin_log):
    save_on_the_screen(live_bridge, oscam_password="hunter2-not-in-the-log", log_level="debug")
    text = plugin_log()
    assert "applying log_level, oscam_password without a reconnect" in text
    assert "hunter2-not-in-the-log" not in text


def test_without_a_broker_connection_the_next_connect_publishes_the_change(live_bridge,
                                                                          factory):
    client = factory.client
    client.fire_disconnect()
    MainLoop.advance(0)
    assert not live_bridge.connected
    client.clear()

    save_on_the_screen(live_bridge, deep_standby_allowed=True, epg_grid_events=0)

    assert client.published == [], "nothing is sent, and nothing is forgotten as if it were"
    assert len(factory.clients) == 1
    client.fire_connect()
    assert client.last(INFO).json()["settings"]["deep_standby_allowed"] is True
    assert "epg_grid" not in client.last(INFO).json()["capabilities"]


def test_a_cmd_config_change_is_not_applied_again_by_the_next_save(live_bridge, factory,
                                                                   plugin_log):
    live_bridge.settings.publish_keys.value = True
    factory.client.fire_message(ROOT + "/cmd/config", json.dumps({"publish_keys": False}))
    MainLoop.advance(0)
    assert live_bridge.settings.publish_keys.value is False
    save_on_the_screen(live_bridge, menu_transparency=40)
    assert "applying menu_transparency without a reconnect" in plugin_log()


def test_a_save_that_fails_to_apply_reconnects_instead(live_bridge, factory, monkeypatch):
    def broken():
        raise RuntimeError("a publisher would not restart")

    monkeypatch.setattr(live_bridge, "_apply_live", broken)
    save_on_the_screen(live_bridge, osd_toast=False)
    assert len(factory.clients) == 2
    assert live_bridge.last_save_reconnected is True


# ------------------------------------------------------------- setting by setting --


def test_the_device_name_goes_out_with_the_announcement_and_discovery(live_bridge, factory):
    save_on_the_screen(live_bridge, friendly_name="Kitchen receiver")
    device = factory.client.last(DEVICE_TOPIC).json()
    assert device["dev"]["name"] == "Kitchen receiver"
    assert factory.client.last(ANNOUNCEMENT).json()["name"] == "Kitchen receiver"


def test_an_emptied_device_name_is_derived_again_as_at_a_start(live_bridge):
    save_on_the_screen(live_bridge, friendly_name="")
    assert live_bridge.value("friendly_name").strip()


def test_a_new_discovery_prefix_moves_discovery_and_the_status_topic(live_bridge, factory):
    client = factory.client
    assert client.last(DEVICE_TOPIC).text != ""
    client.clear()

    save_on_the_screen(live_bridge, ha_discovery_prefix="ha")

    assert client.last(DEVICE_TOPIC).text == "", "the old prefix's device is retracted"
    assert client.last("ha/device/" + NODE + "/config").json()["dev"]
    assert STATUS_TOPIC in client.unsubscriptions
    assert ("ha/status", 1) in client.subscriptions
    assert live_bridge.publisher("menu").handles("ha/status")
    assert not live_bridge.publisher("menu").handles(STATUS_TOPIC)


def test_ha_mode_integration_retracts_discovery_and_says_the_mode(live_bridge, factory):
    client = factory.client
    save_on_the_screen(live_bridge, ha_mode="integration")
    assert client.last(DEVICE_TOPIC).text == ""
    assert client.last(INFO).json()["ha_mode"] == "integration"
    assert client.last(ROOT + "/update") is not None


def test_ha_mode_off_retracts_the_announcement_too(live_bridge, factory):
    save_on_the_screen(live_bridge, ha_mode="off")
    assert factory.client.last(ANNOUNCEMENT).text == ""


def test_a_permission_is_in_info_and_its_buttons_in_discovery(live_bridge, factory):
    save_on_the_screen(live_bridge, deep_standby_allowed=True)
    assert factory.client.last(INFO).json()["settings"]["deep_standby_allowed"] is True
    components = factory.client.last(DEVICE_TOPIC).json()["cmps"]
    assert any("deep_standby" in key for key in components)


def test_keys_switched_off_retract_the_device_triggers(live_bridge, factory):
    trigger = "homeassistant/device_automation/" + NODE + "/red_short/config"
    assert factory.client.last(trigger).text != ""
    save_on_the_screen(live_bridge, publish_keys=False)
    assert factory.client.last(trigger).text == ""
    assert "keys" not in factory.client.last(INFO).json()["capabilities"]


def test_screenshots_switched_off_retract_the_picture(live_bridge, factory):
    save_on_the_screen(live_bridge, screenshot="off")
    assert factory.client.last(ROOT + "/screen").text == ""
    assert live_bridge.publisher("screenshot") is None


def test_the_toast_switched_off_is_gone_and_on_again_is_back(live_bridge, factory):
    assert "toast" in live_bridge.capabilities()
    save_on_the_screen(live_bridge, osd_toast=False)
    assert live_bridge.publisher("toast") is None
    assert "toast" not in factory.client.last(INFO).json()["capabilities"]
    save_on_the_screen(live_bridge, osd_toast=True)
    assert live_bridge.publisher("toast") is not None
    assert "toast" in factory.client.last(INFO).json()["capabilities"]
    assert len(factory.clients) == 1


def test_the_log_level_is_set_at_once(live_bridge):
    import logging

    save_on_the_screen(live_bridge, log_level="error")
    assert logging.getLogger("MQTTBridge").level == logging.ERROR


def test_wake_on_lan_is_armed_at_once(live_bridge, monkeypatch):
    armed = []
    monkeypatch.setattr(bridge_module.wol, "arm", lambda section=None: armed.append(section))
    save_on_the_screen(live_bridge, wol_arm=True)
    assert armed == [live_bridge.settings]
    # And at every later save, as when every save restarted the bridge: the image's own switch,
    # turned off in its menu meanwhile, is turned on again.
    save_on_the_screen(live_bridge, menu_transparency=60)
    save_on_the_screen(live_bridge)
    assert len(armed) == 3


def test_the_oscam_login_restarts_the_oscam_publisher_only(live_bridge, factory, monkeypatch):
    before = {publisher.name: publisher for publisher in live_bridge._publishers}
    save_on_the_screen(live_bridge, oscam_port=8889)
    after = {publisher.name: publisher for publisher in live_bridge._publishers}
    # Everything but `cmd/config`'s four publishers is the very same object.
    for name, publisher in before.items():
        if name not in ("cam", "oscam", "keys", "screenshot"):
            assert after.get(name) is publisher, name


def test_a_new_oscam_password_is_scrubbed_from_the_log_at_once(live_bridge, plugin_log):
    from MQTTBridge import log as log_module

    secret = "a-new-oscam-secret-7f3e"
    assert log_module.redact(secret) == secret
    save_on_the_screen(live_bridge, oscam_password=secret)
    assert log_module.redact("login " + secret) == "login " + log_module.REDACTED
    # And so in the file: a line that happened to carry it is written without it.
    log_module.get_logger("test").warning("the OSCam login was %s", secret)
    assert secret not in plugin_log()


def test_the_cec_workaround_binds_and_goes_without_a_reconnect(live_bridge, factory):
    from conftest import HdmiCec

    HdmiCec()
    assert live_bridge.publisher("cec_workaround") is None
    save_on_the_screen(live_bridge, cec_standby_workaround=True)
    assert live_bridge.publisher("cec_workaround") is not None
    assert "cec_workaround" in factory.client.last(INFO).json()["capabilities"]
    assert factory.client.last(ROOT + "/cec").json()["count"] == 0

    save_on_the_screen(live_bridge, cec_standby_workaround=False)
    assert live_bridge.publisher("cec_workaround") is None
    assert "cec_workaround" not in factory.client.last(INFO).json()["capabilities"]
    assert factory.client.last(ROOT + "/cec").text == "", "the retained count is retracted"
    assert len(factory.clients) == 1


def test_ha_mode_saved_on_the_screen_republishes_update(live_bridge, factory):
    """With an index held, which release is compatible depends on whether an integration is used.

    The same index and the same change as `cmd/ha_mode`'s test in test_updatecheck.py.
    """
    from updatelab import TEST_KEYS, relay_payload, release, signed

    updates = live_bridge.updates
    updates.keys = TEST_KEYS
    updates.acceptance = False
    updates.clock = lambda: 1790600000
    releases = [release("0.4.0", min_integration="0.4.0"), release("0.3.0", self_update=False)]
    factory.client.fire_message("enigma2mqtt/release_index", relay_payload(*signed(1, releases)),
                                retain=True)
    assert factory.client.last(ROOT + "/update").json()["latest_compatible"] == "0.4.0"

    save_on_the_screen(live_bridge, ha_mode="integration")

    assert factory.client.last(ROOT + "/update").json()["latest_compatible"] == "0.3.0"
    assert len(factory.clients) == 1


def test_ha_mode_set_by_its_command_is_not_applied_again_by_a_save(live_bridge, factory,
                                                                    plugin_log):
    factory.client.fire_message(ROOT + "/cmd/ha_mode", "integration")
    MainLoop.advance(0)
    assert live_bridge.value("ha_mode") == "integration"
    save_on_the_screen(live_bridge, menu_transparency=40)
    assert "applying menu_transparency without a reconnect" in plugin_log()


def test_a_new_bouquet_selection_republishes_service_as_a_fresh_session_would(live_bridge,
                                                                             factory):
    """`service.bouquet` is the channel's bouquet among the selected ones, read at publish."""
    before = factory.client.last(ROOT + "/service").json()
    assert before["bouquet"] == "Ulubione TV"

    save_on_the_screen(live_bridge, bouquets_for_select="Sport (HD)")
    live = factory.client.last(ROOT + "/service").json()
    assert len(factory.clients) == 1

    live_bridge.reload()
    factory.client.fire_connect()
    fresh = factory.client.last(ROOT + "/service").json()
    assert live == fresh
    assert live["bouquet"] is None, "the channel is in no selected bouquet now"


def test_the_epg_grid_follows_its_count_down_to_zero_and_back(live_bridge, factory):
    from conftest import settle

    grid = live_bridge.publisher("epg_grid")
    assert grid is not None and live_bridge.state.grid_slugs
    slugs = list(live_bridge.state.grid_slugs)

    def counts():
        return {slug: factory.client.last(ROOT + "/epg_grid/" + slug).json()["events_per_channel"]
                for slug in slugs}

    assert set(counts().values()) == {4}
    save_on_the_screen(live_bridge, epg_grid_events=2)
    assert live_bridge.publisher("epg_grid") is grid, "a new count rebuilds, it does not restart"
    settle(live_bridge)
    assert set(counts().values()) == {2}, "every grid is built again to the new count"

    save_on_the_screen(live_bridge, epg_grid_events=0)
    assert live_bridge.publisher("epg_grid") is None
    for slug in slugs:
        assert factory.client.last(ROOT + "/epg_grid/" + slug).text == ""
    assert live_bridge.state.grid_slugs == []
    # The channel list goes on walking, and does not start the stopped grid again.
    live_bridge.publisher("channels").refresh()
    assert grid._queue == []

    save_on_the_screen(live_bridge, epg_grid_events=4)
    settle(live_bridge)
    assert live_bridge.publisher("epg_grid") is not None
    assert "epg_grid" in factory.client.last(INFO).json()["capabilities"]
    assert len(factory.clients) == 1


def test_a_new_bouquet_selection_walks_the_bouquets_again(live_bridge, factory, monkeypatch):
    channels = live_bridge.publisher("channels")
    walked = []
    real = channels.refresh

    def counted(force=True):
        walked.append(force)
        return real(force)

    monkeypatch.setattr(channels, "refresh", counted)
    save_on_the_screen(live_bridge, bouquets_for_select="Favourites")
    assert walked == [True]
    assert live_bridge.publisher("channels") is channels


# ------------------------------------------------------------------- the menu --


@pytest.fixture
def world(make_bridge, factory, settings):
    return World(make_bridge, factory, settings)


def test_a_menu_only_save_does_not_grey_the_menu_or_reconnect(world, factory):
    world.open()
    menu = world.publisher
    assert not world.screen.controller.view()["greyed"]
    world.client.clear()

    save_on_the_screen(world.bridge, menu_transparency=60)

    assert len(factory.clients) == 1
    assert world.bridge.publisher("menu") is menu, "the menu and its open screen are kept"
    assert world.screen is not None and not world.screen.controller.view()["greyed"]
    assert world.info()["settings"]["menu_transparency"] == 60
    assert world.client.unsubscriptions == []


def test_a_hotkey_save_rebinds_the_key_without_a_reconnect(world, factory):
    save_on_the_screen(world.bridge, menu_hotkey="red_long")
    ((_domain, _device, key, _bits, _context, action),) = world.receiver.actions.keys
    assert action == "red_long"
    assert world.info()["menu"]["hotkey"] == "red_long"
    assert world.info()["settings"]["menu_hotkey"] == "red_long"
    assert len(factory.clients) == 1


def test_the_menu_switched_off_and_on_without_a_reconnect(world, factory):
    plugins = plugin_component_module.plugins
    assert plugin_module.menu_entry() in plugins.pluginList
    world.open()
    world.client.clear()

    save_on_the_screen(world.bridge, menu_allowed=False)
    MainLoop.advance(0)

    assert len(factory.clients) == 1
    assert world.bridge.publisher("menu") is None
    assert world.screen is None, "the open menu is closed"
    assert plugin_module.menu_entry() not in plugins.pluginList
    assert [entry for entry in world.receiver.actions.bound if entry[0] == CONTEXT] == []
    assert set(world.client.unsubscriptions) >= {MENU_TOPIC, STATUS_TOPIC}
    info = world.info()
    assert "menu" not in info["capabilities"] and info["menu"] is None
    assert info["settings"]["menu_allowed"] is False

    world.client.clear()
    save_on_the_screen(world.bridge, menu_allowed=True)

    assert len(factory.clients) == 1
    assert world.bridge.publisher("menu") is not None
    assert plugins.pluginList.count(plugin_module.menu_entry()) == 1
    assert [entry for entry in world.receiver.actions.bound if entry[0] == CONTEXT]
    assert (MENU_TOPIC, 1) in world.client.subscriptions
    assert (STATUS_TOPIC, 1) in world.client.subscriptions
    info = world.info()
    assert "menu" in info["capabilities"] and info["menu"]["hotkey"] == "help_long"
    # The retained menu the broker hands the new subscription is drawn as before.
    world.open()
    assert world.screen is not None


def test_a_status_heard_under_the_old_prefix_is_forgotten(world):
    world.status("offline")
    assert world.publisher.greyed()
    save_on_the_screen(world.bridge, ha_discovery_prefix="ha")
    assert not world.publisher.greyed()
