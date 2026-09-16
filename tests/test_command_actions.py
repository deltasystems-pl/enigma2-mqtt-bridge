"""Every command that acts on the receiver, and every guard that stops one.

`test_commands.py` covers the dispatcher itself — the retained-command guard,
the size limit, `last_error`. This module is about what the commands *do*, and
about what they refuse to do: a command is „verified by effect", so each test
here asks what changed on the receiver rather than what the handler returned.
"""

from conftest import POLSAT, TVN, TVP1, ConsoleAppContainer, RecordTimerEntry

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
LAST_ERROR = ROOT + "/last_error"


def send(factory, name, payload=b"PRESS", retain=False):
    factory.client.fire_message(ROOT + "/cmd/" + name, payload, retain=retain)


# The three payload shapes `cmd/timer` takes, spelled out once.
ADD_EVENT = ('{"action": "add", "sref": "' + TVP1 + '", "event_id": 27431}').encode("utf-8")
ADD_MANUAL = ('{"action": "add", "sref": "' + TVN + '", "begin": 100, "end": 200,'
              ' "name": "Film"}').encode("utf-8")
DELETE = ('{"action": "delete", "sref": "' + TVP1 + '", "begin": 1789459200,'
          ' "end": 1789460700}').encode("utf-8")


def error(factory):
    entry = factory.client.last(LAST_ERROR)
    return None if entry is None or entry.text == "" else entry.json()["error"]


# --------------------------------------------------------------------- power --


def test_power_on_wakes_the_box(live_bridge, factory, receiver):
    screen = receiver.enter_standby()
    send(factory, "power", b"on")
    assert screen.power_calls == 1
    assert error(factory) is None


def test_power_standby_puts_it_to_sleep(live_bridge, factory, receiver):
    from Screens.Standby import Standby
    from Tools.Notifications import Notifications

    send(factory, "power", b"standby")
    assert Notifications.notifications[-1][0] is Standby


def test_power_toggle_goes_the_other_way(live_bridge, factory, receiver):
    screen = receiver.enter_standby()
    send(factory, "power", b"toggle")
    assert screen.power_calls == 1


def test_power_toggle_from_on_goes_to_standby(live_bridge, factory, receiver):
    from Tools.Notifications import Notifications

    send(factory, "power", b"toggle")
    assert Notifications.notifications


def test_power_on_when_already_on_is_a_no_op(live_bridge, factory, receiver):
    send(factory, "power", b"on")
    assert error(factory) is None


def test_power_publishes_the_state_it_reached(live_bridge, factory, receiver):
    receiver.enter_standby()
    factory.client.clear()
    send(factory, "power", b"on")
    assert factory.client.last(ROOT + "/power").text == "on"


def test_an_unknown_power_state_is_refused(live_bridge, factory):
    send(factory, "power", b"sideways")
    assert "expected on, standby or toggle" in error(factory)


# ------------------------------------------------------------------ shutdown --


def test_restart_gui_opens_the_shutdown_screen(live_bridge, factory, receiver):
    from Screens.Standby import TryQuitMainloop

    send(factory, "restart_gui")
    assert receiver.session.opened[-1] == (TryQuitMainloop, (3,))
    assert error(factory) is None


def test_restart_gui_is_refused_while_recording(live_bridge, factory, receiver):
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    send(factory, "restart_gui")
    assert error(factory) == "the receiver is recording"
    assert receiver.session.opened == []


def test_restart_gui_is_refused_with_a_recording_about_to_start(live_bridge, factory, receiver,
                                                                monkeypatch):
    from MQTTBridge import recording

    monkeypatch.setattr(recording.time, "time", lambda: 1789459200 - 60)
    receiver.add_timer()
    send(factory, "restart_gui")
    assert "starts in 60 s" in error(factory)


def test_deep_standby_is_refused_unless_it_is_allowed(live_bridge, factory, receiver):
    send(factory, "deep_standby")
    assert "switched off" in error(factory)
    assert receiver.session.opened == []


def test_reboot_is_refused_unless_it_is_allowed(live_bridge, factory, receiver):
    send(factory, "reboot")
    assert "switched off" in error(factory)


def test_deep_standby_shuts_the_box_down_when_it_is_allowed(live_bridge, factory, receiver,
                                                            settings):
    from Screens.Standby import TryQuitMainloop

    settings.deep_standby_allowed.value = True
    send(factory, "deep_standby")
    assert receiver.session.opened[-1] == (TryQuitMainloop, (1,))


def test_deep_standby_says_goodbye_before_it_goes(live_bridge, factory, settings):
    """The difference between a consumer knowing and a consumer waiting out the
    keepalive to guess."""
    settings.deep_standby_allowed.value = True
    factory.client.clear()
    send(factory, "deep_standby")
    assert factory.client.last(ROOT + "/availability").text == "offline"
    assert factory.client.client_disconnected()


def test_reboot_reboots_when_it_is_allowed(live_bridge, factory, receiver, settings):
    from Screens.Standby import TryQuitMainloop

    settings.deep_standby_allowed.value = True
    send(factory, "reboot")
    assert receiver.session.opened[-1] == (TryQuitMainloop, (2,))


def test_deep_standby_is_still_refused_while_recording(live_bridge, factory, receiver, settings):
    settings.deep_standby_allowed.value = True
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    send(factory, "deep_standby")
    assert error(factory) == "the receiver is recording"


def test_nothing_is_disconnected_when_the_shutdown_is_refused(live_bridge, factory, receiver):
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    send(factory, "restart_gui")
    assert live_bridge.client is not None


# ----------------------------------------------------------------------- zap --


def test_zap_takes_a_bare_service_reference(live_bridge, factory, receiver):
    send(factory, "zap", TVN.encode("utf-8"))
    assert receiver.nav.played == [TVN]


def test_zap_takes_an_object_with_a_reference(live_bridge, factory, receiver):
    send(factory, "zap", b'{"sref": "' + TVN.encode("utf-8") + b'"}')
    assert receiver.nav.played == [TVN]


def test_zap_takes_a_channel_name(live_bridge, factory, receiver):
    send(factory, "zap", b'{"name": "Polsat Sport"}')
    assert receiver.nav.played == [POLSAT]


def test_zap_by_an_unknown_name_is_refused(live_bridge, factory, receiver):
    send(factory, "zap", b'{"name": "nonexistent"}')
    assert "no channel called 'nonexistent'" in error(factory)
    assert receiver.nav.played == []


def test_zap_with_nothing_to_go_on_is_refused(live_bridge, factory):
    send(factory, "zap", b"")
    assert "no service reference or name given" in error(factory)


def test_zap_watches_for_the_service_to_echo(live_bridge, factory, receiver):
    send(factory, "zap", TVN.encode("utf-8"))
    publisher = live_bridge.publisher("service")
    assert publisher._verify.timer.started == (5000, True)


def test_a_zap_that_never_lands_ends_on_last_error(live_bridge, factory, receiver):
    send(factory, "zap", TVN.encode("utf-8"))
    receiver.nav.sref = TVP1  # the box stayed where it was
    live_bridge.publisher("service")._verify.timer.fire()
    assert "did not tune to" in error(factory)


# -------------------------------------------------------------------- volume --


def test_volume_sets_the_level(live_bridge, factory, receiver):
    send(factory, "volume", b"42")
    assert receiver.volume.getVolume() == 42


def test_volume_publishes_what_it_reached(live_bridge, factory, receiver):
    factory.client.clear()
    send(factory, "volume", b"42")
    assert factory.client.last(ROOT + "/volume").json()["level"] == 42


def test_volume_is_clamped_rather_than_refused(live_bridge, factory, receiver, plugin_log):
    send(factory, "volume", b"300")
    assert receiver.volume.getVolume() == 100
    assert "clamped" in plugin_log()
    assert error(factory) is None


def test_volume_takes_a_number_in_an_object(live_bridge, factory, receiver):
    send(factory, "volume", b'{"level": 12}')
    assert receiver.volume.getVolume() == 12


def test_a_volume_that_is_not_a_number_is_refused(live_bridge, factory, receiver):
    send(factory, "volume", b"loud")
    assert "is not a volume" in error(factory)
    assert receiver.volume.getVolume() == 35


def test_mute_mutes(live_bridge, factory, receiver):
    send(factory, "mute", b"ON")
    assert receiver.volume.isMuted() is True


def test_mute_off_unmutes(live_bridge, factory, receiver):
    receiver.volume_control.volMute()
    send(factory, "mute", b"OFF")
    assert receiver.volume.isMuted() is False


def test_mute_is_spelled_however_the_sender_likes(live_bridge, factory, receiver):
    send(factory, "mute", b"true")
    assert receiver.volume.isMuted() is True


def test_an_unknown_mute_state_is_refused(live_bridge, factory, receiver):
    send(factory, "mute", b"maybe")
    assert "expected ON or OFF" in error(factory)


# ----------------------------------------------------------------------- key --


def test_a_key_is_injected(live_bridge, factory, receiver):
    send(factory, "key", b"KEY_INFO")
    assert [flag for _d, key, flag in receiver.actions.pressed if key == 358] == [0, 1]


def test_a_long_key_is_injected(live_bridge, factory, receiver):
    send(factory, "key", b'{"key": "KEY_INFO", "long": true}')
    assert [flag for _d, key, flag in receiver.actions.pressed if key == 358] == [0, 3, 1]


def test_an_unknown_key_is_refused(live_bridge, factory, receiver):
    send(factory, "key", b"KEY_TELEPORT")
    assert "unknown key 'KEY_TELEPORT'" in error(factory)


def test_a_key_with_no_name_is_refused(live_bridge, factory):
    send(factory, "key", b"")
    assert "no key given" in error(factory)


def test_a_flood_of_injected_keys_is_capped(live_bridge, factory, receiver):
    """Forty in a second: twenty are published and the rest are dropped."""
    for _ in range(40):
        send(factory, "key", b"KEY_RED")
    assert len(factory.client.all_for(ROOT + "/key")) == 20


# ------------------------------------------------------------------- message --


def test_a_message_is_put_on_the_screen(live_bridge, factory):
    from Tools.Notifications import Notifications

    send(factory, "message", b'{"text": "Test MQTT Bridge"}')
    assert Notifications.popups[-1]["text"] == "Test MQTT Bridge"
    assert Notifications.popups[-1]["timeout"] == 10


def test_a_message_replaces_the_one_before_it(live_bridge, factory):
    """Otherwise they queue, and somebody dismisses them one at a time."""
    from Tools.Notifications import Notifications

    send(factory, "message", b'{"text": "one"}')
    send(factory, "message", b'{"text": "two"}')
    assert Notifications.removed == ["mqttbridge", "mqttbridge"]
    assert Notifications.popups[-1]["id"] == "mqttbridge"


def test_a_message_carries_its_type_and_timeout(live_bridge, factory):
    from Screens.MessageBox import MessageBox
    from Tools.Notifications import Notifications

    send(factory, "message", b'{"text": "careful", "type": "warning", "timeout": 5}')
    assert Notifications.popups[-1]["type"] == MessageBox.TYPE_WARNING
    assert Notifications.popups[-1]["timeout"] == 5


def test_a_bare_string_is_a_message_too(live_bridge, factory):
    from Tools.Notifications import Notifications

    send(factory, "message", b"Dobranoc")
    assert Notifications.popups[-1]["text"] == "Dobranoc"


def test_polish_survives_the_round_trip(live_bridge, factory):
    from Tools.Notifications import Notifications

    send(factory, "message", '{"text": "Nieobecność włączona"}'.encode())
    assert Notifications.popups[-1]["text"] == "Nieobecność włączona"


def test_a_long_message_is_truncated_rather_than_refused(live_bridge, factory):
    from Tools.Notifications import Notifications

    send(factory, "message", b'{"text": "' + b"x" * 900 + b'"}')
    assert len(Notifications.popups[-1]["text"]) == 500
    assert error(factory) is None


def test_an_empty_message_is_refused(live_bridge, factory):
    send(factory, "message", b'{"text": "  "}')
    assert "empty" in error(factory)


def test_an_unknown_message_type_is_refused(live_bridge, factory):
    send(factory, "message", b'{"text": "hello", "type": "shouting"}')
    assert "unknown message type" in error(factory)


def test_a_message_timeout_that_is_not_a_number_is_refused(live_bridge, factory):
    send(factory, "message", b'{"text": "hello", "timeout": "soon"}')
    assert "is not a number of seconds" in error(factory)


def test_a_popup_the_image_will_not_show_is_reported(live_bridge, factory):
    from Tools.Notifications import Notifications

    Notifications.raises = True
    send(factory, "message", b'{"text": "hello"}')
    assert error(factory) is not None


# --------------------------------------------------------------------- timer --


def test_a_timer_is_added_from_an_event(live_bridge, factory, receiver):
    send(factory, "timer", ADD_EVENT)
    assert receiver.nav.RecordTimer.timer_list[0].name == "Wiadomości"


def test_adding_a_timer_republishes_the_list(live_bridge, factory, receiver):
    factory.client.clear()
    send(factory, "timer", ADD_EVENT)
    live_bridge.publisher("timers")._coalesce.timer.fire()
    assert factory.client.last(ROOT + "/timers").json()[0]["name"] == "Wiadomości"


def test_a_manual_timer_is_added(live_bridge, factory, receiver):
    send(factory, "timer", ADD_MANUAL)
    assert receiver.nav.RecordTimer.timer_list[0].name == "Film"


def test_a_timer_is_deleted(live_bridge, factory, receiver):
    timer = receiver.add_timer()
    send(factory, "timer", DELETE)
    assert receiver.nav.RecordTimer.removed == [timer]


def test_a_timer_with_no_action_is_refused(live_bridge, factory):
    send(factory, "timer", ('{"sref": "' + TVP1 + '"}').encode())
    assert "unknown timer action" in error(factory)


def test_a_timer_with_no_service_is_refused(live_bridge, factory):
    send(factory, "timer", b'{"action": "add", "event_id": 1}')
    assert "no sref in the timer" in error(factory)


def test_an_add_with_neither_an_event_nor_a_window_is_refused(live_bridge, factory):
    send(factory, "timer", ('{"action": "add", "sref": "' + TVP1 + '"}').encode())
    assert "needs an event_id, or a begin and an end" in error(factory)


def test_a_delete_with_no_window_is_refused(live_bridge, factory):
    send(factory, "timer", ('{"action": "delete", "sref": "' + TVP1 + '"}').encode())
    assert "identified by sref, begin and end" in error(factory)


def test_a_timer_that_is_not_json_is_refused(live_bridge, factory):
    send(factory, "timer", b"add one please")
    assert "takes a JSON object" in error(factory)


# -------------------------------------------------------------------- record --


def test_record_start_records_what_is_playing(live_bridge, factory, receiver):
    send(factory, "record", b"start")
    assert str(receiver.nav.RecordTimer.timer_list[0].service_ref) == TVP1


def test_record_start_publishes_the_recording_state(live_bridge, factory, receiver):
    factory.client.clear()
    send(factory, "record", b"start")
    receiver.nav.RecordTimer.timer_list[0].state = RecordTimerEntry.StateRunning
    live_bridge.publisher("recording")._coalesce.timer.fire()
    assert factory.client.last(ROOT + "/recording").json()["active"] != []


def test_record_stop_removes_the_running_recording(live_bridge, factory, receiver):
    timer = receiver.add_timer(state=RecordTimerEntry.StateRunning)
    send(factory, "record", b"stop")
    assert receiver.nav.RecordTimer.removed == [timer]


def test_record_stop_with_nothing_recording_is_refused(live_bridge, factory):
    send(factory, "record", b"stop")
    assert "nothing is being recorded" in error(factory)


def test_an_unknown_record_action_is_refused(live_bridge, factory):
    send(factory, "record", b"pause")
    assert "expected start or stop" in error(factory)


# ---------------------------------------------------------------- screenshot --


def test_screenshot_runs_grab(live_bridge, factory, tmp_path):
    live_bridge.publisher("screenshot").path = str(tmp_path / "shot.jpg")
    send(factory, "screenshot")
    assert ConsoleAppContainer.instances[-1].commands
    assert error(factory) is None


def test_a_screenshot_too_soon_after_the_last_one_is_refused(live_bridge, factory, tmp_path):
    live_bridge.publisher("screenshot").path = str(tmp_path / "shot.jpg")
    send(factory, "screenshot")
    ConsoleAppContainer.instances[-1].finish(0)
    send(factory, "screenshot")
    assert "at most one every 5 s" in error(factory)


def test_a_screenshot_on_a_box_that_cannot_take_one(make_bridge, factory, settings, receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.screenshot.value = "off"
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    send(factory, "screenshot")
    assert "not available" in error(factory)


# ------------------------------------------------------------------ epg_grid --


def test_epg_grid_rebuilds_every_bouquet(live_bridge, factory, receiver):
    from conftest import Event

    receiver.epg.events[TVP1] = [Event(7, 10, 20, "Rebuilt")]
    factory.client.clear()
    send(factory, "epg_grid")
    publisher = live_bridge.publisher("epg_grid")
    for _ in range(3):
        if publisher._queue:
            publisher._step.timer.fire()
    payload = factory.client.last(ROOT + "/epg_grid/ulubione_tv").json()
    assert payload["channels"][0]["events"][0]["title"] == "Rebuilt"


def test_epg_grid_is_refused_when_the_grid_is_off(make_bridge, factory, settings, receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.epg_grid_events.value = 0
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    send(factory, "epg_grid")
    assert "switched off" in error(factory)


# ----------------------------------------------------------------- discovery --


def test_discovery_republishes_the_channel_list(live_bridge, factory, receiver):
    import conftest

    receiver.service_center.contents[conftest.FIRST_BOUQUET].append((POLSAT, "Polsat Sport"))
    factory.client.clear()
    send(factory, "discovery")
    channels = factory.client.last(ROOT + "/channels").json()["bouquets"][0]["channels"]
    assert [c["sref"] for c in channels] == [TVP1, TVN, POLSAT]


def test_discovery_republishes_the_device_payload(live_bridge, factory):
    factory.client.clear()
    send(factory, "discovery")
    assert factory.client.last("homeassistant/device/" + NODE + "/config") is not None


# --------------------------------------------------------------------- reset --


def test_reset_retracts_the_grids_and_the_screenshot(live_bridge, factory, tmp_path):
    found = live_bridge.publisher("screenshot")
    found.path = str(tmp_path / "shot.jpg")
    found.capture(commanded=True)
    with open(found.path, "wb") as handle:
        handle.write(b"\xff\xd8jpeg")
    ConsoleAppContainer.instances[-1].finish(0)

    factory.client.clear()
    send(factory, "reset")
    retracted = [entry.topic for entry in factory.client.published if entry.text == ""]
    assert ROOT + "/screen" in retracted
    assert ROOT + "/epg_grid/ulubione_tv" in retracted
    assert "homeassistant/device/" + NODE + "/config" in retracted


def test_reset_puts_everything_straight_back(live_bridge, factory):
    factory.client.clear()
    send(factory, "reset")
    assert factory.client.last(ROOT + "/availability").text == "online"
    assert factory.client.last(ROOT + "/service").text != ""
    assert factory.client.last(ROOT + "/channels").text != ""
    assert factory.client.last("homeassistant/device/" + NODE + "/config").text != ""


# --------------------------------------------------------- verified by effect --


def test_a_command_that_worked_clears_a_previous_error(live_bridge, factory, receiver):
    send(factory, "power", b"sideways")
    assert error(factory) is not None
    send(factory, "power", b"on")
    assert factory.client.last(LAST_ERROR).text == ""


def test_an_unknown_command_is_an_error_not_a_crash(live_bridge, factory):
    send(factory, "fly", b"PRESS")
    assert "unknown command" in error(factory)


def test_a_retained_command_is_never_obeyed(live_bridge, factory, receiver):
    """🔴 A retained `deep_standby` is a receiver that will not stay on."""
    send(factory, "zap", TVN.encode("utf-8"), retain=True)
    assert receiver.nav.played == []
    assert factory.client.last(LAST_ERROR) is None


def test_an_oversized_command_is_discarded(live_bridge, factory, receiver):
    send(factory, "zap", b"x" * 5000)
    assert receiver.nav.played == []
    assert factory.client.last(LAST_ERROR) is None


def test_a_handler_that_raises_ends_on_last_error(live_bridge, factory, monkeypatch):
    from MQTTBridge import power as power_module

    def explode():
        raise RuntimeError("the standby screen fell over")

    monkeypatch.setattr(power_module, "wake", explode)
    send(factory, "power", b"on")
    assert "RuntimeError" in error(factory)
