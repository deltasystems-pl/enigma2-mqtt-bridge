"""The Home Assistant discovery payloads, against what HA 2026.9 actually accepts.

Home Assistant is not a test dependency of this plugin — it would be a hundred
megabytes of it to check a dictionary — so these assertions are written against
its source, read at the time this was built:

* the device payload's top level takes `dev`, `o`, `cmps`, the availability
  keys, `stat_t`, `cmd_t`, `qos`, `encoding` and `msg_exp_int`, and **an unknown
  key there drops every component in the payload**;
* each component needs `p`, and an entity component needs `uniq_id`;
* `object_id` is not in the schema on this version; `default_entity_id` is, and
  it wants a full `domain.object` string;
* a component reduced to `{"p": "<platform>"}` is how one is removed, and
  leaving it out entirely removes nothing;
* device triggers keep only `atype`, `dev`, `t`, `type`, `stype`, `pl`,
  `val_tpl`, `qos` and `encoding` — anything else, availability included, is
  silently dropped.
"""

import datetime
import json

import conftest
import pytest

from MQTTBridge import discovery

NODE = "vuuno4kse_005301"
DEVICE_TOPIC = "homeassistant/device/" + NODE + "/config"
ROOT = "enigma2/" + NODE

# Exactly what the device-payload schema allows at the top level.
TOP_LEVEL = {
    "dev", "o", "cmps", "avty_t", "avty", "avty_mode", "avty_tpl", "pl_avail",
    "pl_not_avail", "stat_t", "cmd_t", "qos", "e", "msg_exp_int",
}

PLATFORMS = {
    "switch", "sensor", "binary_sensor", "number", "select", "image", "button",
    "device_automation",
}


def payload(factory):
    return factory.client.last(DEVICE_TOPIC).json()


def components(factory):
    return payload(factory)["cmps"]


def test_one_device_payload_carries_everything(live_bridge, factory):
    assert factory.client.last(DEVICE_TOPIC) is not None
    assert factory.client.last(DEVICE_TOPIC).retain is True


def test_no_key_outside_the_schema_reaches_the_top_level(live_bridge, factory):
    """🔴 One unknown key there and Home Assistant drops every entity in the payload."""
    assert set(payload(factory)) <= TOP_LEVEL


def test_the_device_block_identifies_the_box(live_bridge, factory):
    device = payload(factory)["dev"]
    assert device["ids"] == [NODE]
    assert device["name"] == "Living room receiver"
    assert device["mdl"] == "vuuno4kse"
    assert device["mf"] == "Vu+"
    assert "MQTT Bridge" in device["sw"]


def test_the_manufacturer_is_guessed_from_the_machine_name():
    assert discovery.manufacturer("vuuno4kse") == "Vu+"
    assert discovery.manufacturer("gbquad4k") == "GigaBlue"
    assert discovery.manufacturer("zgemmah9combo") == "Zgemma"
    # Not a guess when there is nothing to go on.
    assert discovery.manufacturer("something-new") == "Enigma2"
    assert discovery.manufacturer("") == "Enigma2"


def test_the_configuration_url_is_the_boxs_own_web_interface(live_bridge, factory, monkeypatch):
    from MQTTBridge import boxinfo

    monkeypatch.setattr(boxinfo, "local_ip", lambda host=None: "192.0.2.12")
    live_bridge.publish_discovery()
    assert payload(factory)["dev"]["cu"] == "http://192.0.2.12/"


def test_a_box_that_does_not_know_its_address_offers_no_url(live_bridge, factory, monkeypatch):
    """🔴 `http:///` is not a URL, and Home Assistant would reject the device."""
    from MQTTBridge import boxinfo

    monkeypatch.setattr(boxinfo, "local_ip", lambda host=None: "")
    live_bridge.publish_discovery()
    assert "cu" not in payload(factory)["dev"]


def test_the_origin_block_names_this_plugin(live_bridge, factory):
    origin = payload(factory)["o"]
    assert origin["name"] == "enigma2-mqtt-bridge"
    assert origin["url"].startswith("https://github.com/")
    assert origin["sw"]


def test_availability_is_shared_by_every_component(live_bridge, factory):
    assert payload(factory)["avty_t"] == ROOT + "/availability"


def test_commands_are_published_at_qos_one(live_bridge, factory):
    """The contract's QoS for commands; the top-level key reaches every component."""
    assert payload(factory)["qos"] == 1


def test_every_component_names_its_platform(live_bridge, factory):
    for key, component in components(factory).items():
        assert component["p"] in PLATFORMS, key


def test_every_entity_component_has_a_unique_id(live_bridge, factory):
    """🔴 Without one, Home Assistant refuses the whole payload."""
    for key, component in components(factory).items():
        assert component["uniq_id"] == NODE + "_" + key


def test_every_component_names_the_entity_id_it_wants(live_bridge, factory):
    for key, component in components(factory).items():
        entity_id = component["def_ent_id"]
        # A value with no dot leaves nothing to keep and the entity is created
        # as `<platform>.unnamed_device`.
        assert "." in entity_id
        assert entity_id == component["p"] + ".living_room_receiver_" + key


def test_object_id_is_not_used(live_bridge, factory):
    """It was accepted and silently discarded on this version — a no-op."""
    for component in components(factory).values():
        assert "obj_id" not in component
        assert "object_id" not in component


def test_the_expected_entities_are_all_there(live_bridge, factory):
    assert set(components(factory)) == {
        "power", "channel", "program", "next_program", "recording", "active_recordings",
        "next_timer", "volume", "mute", "channel_select", "screen", "screenshot",
        "restart_gui", "refresh_discovery", "snr", "agc", "ber", "recording_disk", "uptime",
        "process_memory", "process_memory_peak", "process_threads", "process_open_files",
        "process_started",
    }


def test_the_power_switch_speaks_the_power_topic(live_bridge, factory):
    switch = components(factory)["power"]
    assert switch["p"] == "switch"
    assert switch["stat_t"] == ROOT + "/power"
    assert switch["cmd_t"] == ROOT + "/cmd/power"
    assert (switch["pl_on"], switch["pl_off"]) == ("on", "standby")


def test_the_channel_sensor_carries_the_documented_attributes(live_bridge, factory):
    sensor = components(factory)["channel"]
    assert sensor["stat_t"] == ROOT + "/service"
    assert sensor["json_attr_t"] == ROOT + "/service"
    for field in ("sref", "bouquet", "provider", "width", "height"):
        assert "'" + field + "'" in sensor["json_attr_tpl"]
    # The name is the state, not an attribute.
    assert "'name'" not in sensor["json_attr_tpl"]


def test_the_channel_sensor_survives_an_absent_key(live_bridge, factory):
    """An absent key renders empty, which a sensor reads as „ignore this"."""
    assert "default(none)" in components(factory)["channel"]["val_tpl"]


def test_the_programme_sensor_does_not_walk_into_a_null(live_bridge, factory):
    """`value_json.now.title` on a null `now` is a template error, and a template
    error leaves the last programme on screen for ever."""
    sensor = components(factory)["program"]
    assert sensor["val_tpl"] == "{{ value_json.now.title if value_json.now else none }}"


def test_the_programme_attributes_flatten_the_next_programme(live_bridge, factory):
    template = components(factory)["program"]["json_attr_tpl"]
    for field in ("'begin'", "'end'", "'event_id'", "'short'", "'long'",
                  "'next_title'", "'next_begin'", "'next_end'"):
        assert field in template


def test_the_next_timer_is_a_timestamp_home_assistant_will_take(live_bridge, factory):
    """🔴 A timestamp sensor refuses epoch seconds and refuses a time with no zone.

    Rendered rather than matched as a string. The string was matched for two
    releases and said everything a reader wanted to hear — `timestamp_utc` is in
    there, `+00:00` is in there — while the template appended a second offset to
    a filter that already ends in one. Home Assistant answers `…+00:00+00:00`
    with „Invalid state message" and stores nothing, so the sensor read unknown
    for ever and no test could tell.
    """
    sensor = components(factory)["next_timer"]
    assert sensor["dev_cla"] == "timestamp"

    rendered = conftest.render_value_template(
        sensor["val_tpl"], {"next": {"begin": 1789042109}}
    )
    assert datetime.datetime.fromisoformat(rendered) == datetime.datetime(
        2026, 9, 10, 12, 8, 29, tzinfo=datetime.timezone.utc
    )

    # And the „no timer" case is the literal None, which Home Assistant's MQTT
    # sensor turns into the unknown state before it tries to parse anything.
    assert conftest.render_value_template(sensor["val_tpl"], {"next": None}) == "None"


def test_the_volume_number_is_a_slider_from_zero_to_a_hundred(live_bridge, factory):
    number = components(factory)["volume"]
    assert number["p"] == "number"
    assert (number["min"], number["max"], number["step"]) == (0, 100, 1)
    assert number["mode"] == "slider"
    assert number["cmd_t"] == ROOT + "/cmd/volume"
    assert number["val_tpl"] == "{{ value_json.level }}"


def test_the_channel_select_offers_the_channels_of_the_configured_bouquets(live_bridge, factory):
    select = components(factory)["channel_select"]
    assert select["p"] == "select"
    assert select["ops"] == ["TVP 1 HD", "TVN HD", "Polsat Sport"]
    assert select["cmd_t"] == ROOT + "/cmd/zap"


def test_the_channel_select_escapes_the_name_it_sends(live_bridge, factory):
    """🔴 A channel called 4\" News would otherwise publish broken JSON."""
    template = components(factory)["channel_select"]["cmd_tpl"]
    assert template == '{"name": {{ value | tojson }}}'


def test_a_box_with_no_channels_offers_no_select(make_bridge, factory, settings, receiver,
                                                 monkeypatch):
    from MQTTBridge import channels as channels_module

    monkeypatch.setattr(channels_module, "read_bouquets", lambda wanted=None: (None, []))
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert "channel_select" not in components(factory)


def test_the_screen_is_an_image_entity(live_bridge, factory):
    image = components(factory)["screen"]
    assert image["p"] == "image"
    assert image["img_t"] == ROOT + "/screen"
    assert image["cont_type"] == "image/jpeg"
    # 🔴 `url_t` and `cont_type` together are an error, and `img_t` and `url_t`
    # are mutually exclusive.
    assert "url_t" not in image


def test_the_deep_standby_buttons_are_absent_until_they_are_allowed(live_bridge, factory):
    """A button that is always refused is a button somebody reports as broken."""
    assert "deep_standby" not in components(factory)
    assert "reboot" not in components(factory)


def test_the_deep_standby_buttons_appear_when_they_are_allowed(live_bridge, factory, settings):
    settings.deep_standby_allowed.value = True
    live_bridge.publish_discovery()
    assert components(factory)["deep_standby"]["cmd_t"] == ROOT + "/cmd/deep_standby"
    assert components(factory)["reboot"]["cmd_t"] == ROOT + "/cmd/reboot"


def test_the_signal_sensors_are_diagnostic_and_off_by_default(live_bridge, factory):
    for key in ("snr", "agc", "ber"):
        sensor = components(factory)[key]
        assert sensor["ent_cat"] == "diagnostic"
        assert sensor["en"] is False


def test_the_signal_percentages_carry_their_unit(live_bridge, factory):
    assert components(factory)["snr"]["unit_of_meas"] == "%"
    assert components(factory)["agc"]["unit_of_meas"] == "%"
    # The error count is not a percentage of anything.
    assert "unit_of_meas" not in components(factory)["ber"]


def test_the_uptime_sensor_is_a_duration_in_seconds(live_bridge, factory):
    sensor = components(factory)["uptime"]
    assert sensor["dev_cla"] == "duration"
    assert sensor["unit_of_meas"] == "s"
    assert sensor["stat_t"] == ROOT + "/info"


def test_a_component_whose_capability_is_missing_is_not_announced(make_bridge, factory, settings,
                                                                  receiver, monkeypatch):
    from MQTTBridge import volume

    monkeypatch.setattr(volume, "_hardware", lambda: None)
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert "volume" not in components(factory)
    assert "mute" not in components(factory)


def test_a_component_that_used_to_be_announced_is_removed_by_name(live_bridge, factory):
    """🔴 Leaving it out of a republished payload removes nothing at all."""
    live_bridge.state.set_component_keys({"volume": "number", "gone": "sensor"})
    live_bridge.publish_discovery()
    assert components(factory)["gone"] == {"p": "sensor"}
    # And the one that is still announced is a whole component, not a removal.
    assert len(components(factory)["volume"]) > 1


def test_a_removal_is_not_remembered_as_a_component(live_bridge, factory):
    live_bridge.state.set_component_keys({"gone": "sensor"})
    live_bridge.publish_discovery()
    assert "gone" not in live_bridge.state.component_keys


def test_the_announced_components_are_remembered(live_bridge):
    keys = live_bridge.state.component_keys
    assert keys["power"] == "switch"
    assert keys["screen"] == "image"


def test_eight_device_triggers_are_published(live_bridge, factory):
    topics = [t for t in factory.client.topics() if "device_automation" in t]
    assert len(set(topics)) == 8


def test_the_triggers_are_the_colour_keys_in_both_lengths(live_bridge, factory):
    names = sorted(t.split("/")[-2] for t in set(factory.client.topics())
                   if "device_automation" in t)
    assert names == [
        "blue_long", "blue_short", "green_long", "green_short",
        "red_long", "red_short", "yellow_long", "yellow_short",
    ]


def test_a_trigger_payload_has_what_home_assistant_keeps(live_bridge, factory):
    topic = "homeassistant/device_automation/" + NODE + "/red_long/config"
    trigger = factory.client.last(topic).json()
    assert trigger == {
        "atype": "trigger",
        "type": "button_long_press",
        "stype": "red",
        "t": ROOT + "/key",
        "val_tpl": "{{ value_json.key }}_{{ value_json.press }}",
        "pl": "KEY_RED_long",
        "dev": {"ids": [NODE], "name": "Living room receiver"},
    }


def test_a_trigger_is_matched_by_the_payload_the_key_topic_carries(live_bridge, factory):
    topic = "homeassistant/device_automation/" + NODE + "/green_short/config"
    trigger = factory.client.last(topic).json()
    # What the plugin actually publishes on a green press, through the template.
    published = {"key": "KEY_GREEN", "press": "short"}
    rendered = published["key"] + "_" + published["press"]
    assert rendered == trigger["pl"]


def test_no_triggers_when_the_remote_is_not_watched(make_bridge, factory, settings, receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.publish_keys.value = False
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert [t for t in factory.client.topics() if "device_automation" in t] == []


def test_the_discovery_prefix_is_a_setting(make_bridge, factory, settings, receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.ha_discovery_prefix.value = "ha"
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert factory.client.last("ha/device/" + NODE + "/config") is not None


def test_nothing_is_published_in_integration_mode(make_bridge, factory, settings, receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.ha_mode.value = "integration"
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert [t for t in factory.client.topics() if t.startswith("homeassistant/")] == []
    # But the state topics and the announcement are published as ever.
    assert factory.client.last(ROOT + "/service") is not None
    assert factory.client.last("enigma2mqtt/discovery/" + NODE + "/config") is not None


def test_switching_to_integration_mode_retracts_the_payloads(live_bridge, factory):
    factory.client.clear()
    live_bridge.set_ha_mode("integration")
    retracted = [entry for entry in factory.client.published
                 if entry.topic.startswith("homeassistant/")]
    assert len(retracted) == 9  # the device payload and the eight triggers
    assert all(entry.text == "" and entry.retain for entry in retracted)


def test_switching_back_republishes_them(live_bridge, factory):
    live_bridge.set_ha_mode("integration")
    factory.client.clear()
    live_bridge.set_ha_mode("discovery")
    assert factory.client.last(DEVICE_TOPIC).text != ""


def test_the_payload_is_valid_json_with_polish_in_it(live_bridge, factory, receiver):
    receiver.service_center.contents[conftest.BOUQUET_ROOT] = [
        (conftest.FIRST_BOUQUET, "Kanały Główne"),
    ]
    live_bridge.publisher("channels").refresh()
    live_bridge.publish_discovery()
    text = factory.client.last(DEVICE_TOPIC).text
    assert json.loads(text)


# ------------------------------------------------ what a connect retracts --
#
# The stale-topic retraction runs before the first publish of every connect and
# every reload. An empty retained device payload is a deletion in Home
# Assistant — of the device and every entity on it — so whatever the session is
# about to publish again must not be emptied first, however briefly.

TRIGGERS = [
    "homeassistant/device_automation/" + NODE + "/" + colour + "_" + press + "/config"
    for colour in ("red", "green", "yellow", "blue") for press in ("short", "long")
]
OURS = [DEVICE_TOPIC] + TRIGGERS


def emptied(published):
    return [entry.topic for entry in published if entry.retain and entry.text == ""]


def reconnect(bridge, factory):
    factory.client.clear()
    factory.client.fire_disconnect()
    factory.client.fire_connect()
    return list(factory.client.published)


def reload(bridge, factory):
    old = factory.client
    old.clear()
    bridge.reload()
    factory.client.fire_connect()
    return list(old.published) + list(factory.client.published)


def restart(bridge, factory, make_bridge, receiver):
    """A new process on the same state file — the connect after a GUI restart."""
    factory.client.fire_disconnect()
    bridge.stop()
    fresh = make_bridge(session=receiver.session)
    fresh.start()
    factory.client.fire_connect()
    return list(factory.client.published)


def test_a_reconnect_does_not_retract_the_discovery_payloads(live_bridge, factory):
    assert factory.client.last(DEVICE_TOPIC).text != ""
    published = reconnect(live_bridge, factory)

    assert [topic for topic in emptied(published) if topic.startswith("homeassistant/")] == []
    assert factory.client.last(DEVICE_TOPIC).text != ""


def test_a_reload_does_not_retract_the_discovery_payloads(live_bridge, factory):
    published = reload(live_bridge, factory)

    assert [topic for topic in emptied(published) if topic.startswith("homeassistant/")] == []
    assert factory.client.last(DEVICE_TOPIC).text != ""


def test_a_restart_does_not_retract_the_discovery_payloads(live_bridge, factory, make_bridge,
                                                          receiver):
    published = restart(live_bridge, factory, make_bridge, receiver)

    assert [topic for topic in emptied(published) if topic.startswith("homeassistant/")] == []


def test_the_failed_removal_restart_does_not_retract_the_discovery_payloads(live_bridge,
                                                                            factory):
    old = factory.client
    old.clear()
    live_bridge.restart_after_failed_uninstall("uninstall", "opkg is busy")
    factory.client.fire_connect()

    published = list(old.published) + list(factory.client.published)
    assert [topic for topic in emptied(published) if topic.startswith("homeassistant/")] == []


@pytest.mark.parametrize("mode", ["discovery", "integration", "off"])
@pytest.mark.parametrize("how", ["reconnect", "reload", "restart"])
def test_nothing_the_session_republishes_is_emptied_first(make_bridge, factory, settings,
                                                          receiver, mode, how):
    """In every mode, a topic retracted on connect is one that stays retracted."""
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.ha_mode.value = mode
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()

    if how == "reconnect":
        published = reconnect(bridge, factory)
    elif how == "reload":
        published = reload(bridge, factory)
    else:
        published = restart(bridge, factory, make_bridge, receiver)

    refilled = {entry.topic for entry in published if entry.retain and entry.text != ""}
    assert sorted(set(emptied(published)) & refilled) == []


def test_a_new_prefix_retracts_the_payloads_under_the_old_one(live_bridge, factory, settings):
    settings.ha_discovery_prefix.value = "ha"
    published = reload(live_bridge, factory)

    assert sorted(set(emptied(published)) & set(OURS)) == sorted(OURS)
    assert factory.client.last("ha/device/" + NODE + "/config").text != ""


def test_a_new_node_id_retracts_the_payloads_of_the_old_one(live_bridge, factory, settings):
    settings.node_id.value = "vuuno4kse_beef01"
    published = reload(live_bridge, factory)

    assert sorted(set(emptied(published)) & set(OURS)) == sorted(OURS)
    assert factory.client.last("homeassistant/device/vuuno4kse_beef01/config").text != ""


@pytest.mark.parametrize("mode", ["integration", "off"])
def test_leaving_discovery_mode_on_the_setup_screen_retracts_the_payloads(live_bridge, factory,
                                                                         settings, mode):
    """The setup screen reloads; it does not go through `cmd/ha_mode`."""
    settings.ha_mode.value = mode
    published = reload(live_bridge, factory)

    assert sorted(set(emptied(published)) & set(OURS)) == sorted(OURS)
    assert [entry for entry in published
            if entry.topic.startswith("homeassistant/") and entry.text != ""] == []


def test_the_triggers_go_when_the_remote_stops_being_watched(live_bridge, factory, settings):
    """A capability that is gone takes its triggers; the device payload stays."""
    settings.publish_keys.value = False
    published = reload(live_bridge, factory)

    assert sorted(set(emptied(published)) & set(TRIGGERS)) == sorted(TRIGGERS)
    assert DEVICE_TOPIC not in emptied(published)


@pytest.mark.parametrize("capabilities", [[], ["keys"], ["keys", "power", "volume"]])
@pytest.mark.parametrize("prefix", ["homeassistant", "ha"])
def test_the_topic_set_is_what_the_builder_publishes(capabilities, prefix):
    """The retraction trusts this set, so it may not drift from the payloads."""
    built = discovery.build_discovery_components(
        NODE, "Living room receiver", "enigma2", {"capabilities": capabilities}, prefix=prefix
    )
    assert discovery.discovery_topics(prefix, NODE, capabilities) == set(built)


# `cmd/config` and the OpenWebif page apply `publish_keys` without a reconnect,
# so the retraction the connect would have done has to happen on the spot.

def discovery_emptied(published):
    # Every settings apply also re-retracts the switched-off telemetry topics;
    # only the discovery side is this test's business.
    return [topic for topic in emptied(published) if topic.startswith("homeassistant/")]


def keys_off_over_mqtt(bridge, factory):
    factory.client.fire_message(
        ROOT + "/cmd/config",
        b'{"publish_keys":false,"screenshot":"on_zap","screenshot_interval":60}',
    )


def keys_off_directly(bridge, factory):
    """What the OpenWebif page calls for a change inside `cmd/config`'s allowlist."""
    values = bridge.remote_settings()
    values["publish_keys"] = False
    assert bridge.apply_remote_settings(values) is None


@pytest.mark.parametrize("switch_off", [keys_off_over_mqtt, keys_off_directly],
                         ids=["cmd-config", "remote-settings"])
def test_keys_switched_off_in_session_retract_the_triggers_at_once(live_bridge, factory,
                                                                  settings, switch_off):
    factory.client.clear()
    switch_off(live_bridge, factory)

    assert settings.publish_keys.value is False
    assert sorted(set(discovery_emptied(factory.client.published))) == sorted(TRIGGERS)
    assert factory.client.last(DEVICE_TOPIC).text != ""
    for topic in TRIGGERS:
        assert not live_bridge.state.knows(topic)
    # Retracted before the republish, so nothing announced after it is taken back.
    topics = factory.client.topics()
    assert max(topics.index(topic) for topic in TRIGGERS) < topics.index(DEVICE_TOPIC)


def test_keys_switched_back_on_in_session_republish_the_triggers(live_bridge, factory):
    keys_off_directly(live_bridge, factory)
    factory.client.clear()
    values = live_bridge.remote_settings()
    values["publish_keys"] = True
    assert live_bridge.apply_remote_settings(values) is None

    assert discovery_emptied(factory.client.published) == []
    for topic in TRIGGERS:
        assert factory.client.last(topic).text != ""
