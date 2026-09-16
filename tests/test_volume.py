"""Volume and mute, from whichever of the four places changed them."""

from MQTTBridge import volume

NODE = "vuuno4kse_005301"
VOLUME = "enigma2/" + NODE + "/volume"


def test_the_volume_payload_is_a_level_and_a_flag(live_bridge, factory):
    assert factory.client.last(VOLUME).json() == {"level": 35, "muted": False}


def test_the_remote_turning_it_up_publishes(live_bridge, factory, receiver):
    factory.client.clear()
    receiver.volume_control.volUp()
    assert factory.client.last(VOLUME).json()["level"] == 36


def test_the_remote_turning_it_down_publishes(live_bridge, factory, receiver):
    factory.client.clear()
    receiver.volume_control.volDown()
    assert factory.client.last(VOLUME).json()["level"] == 34


def test_muting_from_the_remote_publishes(live_bridge, factory, receiver):
    factory.client.clear()
    receiver.volume_control.volMute()
    assert factory.client.last(VOLUME).json()["muted"] is True


def test_a_change_from_somewhere_else_is_caught_by_the_reconciliation(live_bridge, factory,
                                                                      receiver):
    """OpenWebif writes to the hardware; nothing calls the plugin's wrappers."""
    factory.client.clear()
    receiver.volume.setVolume(70, 70)
    live_bridge.publisher("volume")._ticker.timer.fire()
    assert factory.client.last(VOLUME).json()["level"] == 70


def test_the_reconciliation_is_silent_when_nothing_moved(live_bridge, factory):
    factory.client.clear()
    live_bridge.publisher("volume")._ticker.timer.fire()
    live_bridge.publisher("volume")._ticker.timer.fire()
    assert factory.client.all_for(VOLUME) == []


def test_the_reconciliation_runs_every_five_seconds(live_bridge):
    assert live_bridge.publisher("volume")._ticker.timer.started == (5000, False)


def test_the_wrappers_are_put_back_when_the_publisher_stops(live_bridge, receiver):
    control = receiver.volume_control
    wrapped = control.volUp
    live_bridge.publisher("volume").stop()
    assert control.volUp is not wrapped


def test_wrapping_twice_does_not_stack(live_bridge, receiver, factory):
    publisher = live_bridge.publisher("volume")
    publisher._wrap()
    publisher._wrap()
    factory.client.clear()
    receiver.volume_control.volUp()
    assert len(factory.client.all_for(VOLUME)) == 1


def test_a_wrapper_that_cannot_publish_still_changes_the_volume(live_bridge, receiver,
                                                               monkeypatch, plugin_log):
    publisher = live_bridge.publisher("volume")
    monkeypatch.setattr(publisher, "_publish_now", lambda: 1 / 0)
    receiver.volume_control.volUp()
    assert receiver.volume.getVolume() == 36


def test_setting_the_volume_goes_through_the_hardware(live_bridge, receiver):
    assert volume.set_level(50) is None
    assert receiver.volume.getVolume() == 50


def test_setting_the_volume_shows_the_bar(live_bridge, receiver):
    """A household that cannot see the volume it is changing reports a bug."""
    volume.set_level(50)
    dialog = receiver.volume_control.volumeDialog
    assert dialog.shown == 1
    assert dialog.value == 50


def test_setting_the_volume_saves_it(live_bridge, receiver):
    volume.set_level(50)
    assert receiver.volume_control.saves == 1


def test_setting_the_volume_hides_the_bar_again(live_bridge, receiver):
    volume.set_level(50)
    assert receiver.volume_control.hideVolTimer.started == (3000, True)


def test_the_volume_is_clamped():
    assert volume.clamp(-5) == 0
    assert volume.clamp(500) == 100
    assert volume.clamp(50) == 50


def test_setting_the_volume_without_a_volume_control_still_works(live_bridge, receiver,
                                                                monkeypatch):
    monkeypatch.setattr(volume, "_control", lambda: None)
    assert volume.set_level(42) is None
    assert receiver.volume.getVolume() == 42


def test_setting_the_volume_on_a_box_with_no_volume_at_all(monkeypatch):
    monkeypatch.setattr(volume, "_control", lambda: None)
    monkeypatch.setattr(volume, "_hardware", lambda: None)
    assert "will not let the volume be set" in volume.set_level(42)


def test_muting_when_already_muted_does_nothing(live_bridge, receiver):
    receiver.volume.muted = True
    assert volume.set_muted(True) is None
    assert receiver.volume_control.mute_calls == 0


def test_unmuting_when_not_muted_does_nothing(live_bridge, receiver):
    assert volume.set_muted(False) is None
    assert receiver.volume_control.mute_calls == 0


def test_muting_toggles_once(live_bridge, receiver):
    assert volume.set_muted(True) is None
    assert receiver.volume.isMuted() is True
    assert receiver.volume_control.mute_calls == 1


def test_a_mute_that_the_receiver_refuses_is_reported(live_bridge, receiver):
    """🔴 `volMute` declines at volume zero, and declines without saying so."""
    receiver.volume.setVolume(0, 0)
    error = volume.set_muted(True)
    assert "would not mute" in error


def test_reading_the_volume_off_a_box_that_has_none(monkeypatch):
    monkeypatch.setattr(volume, "_hardware", lambda: None)
    assert volume.read() is None


def test_the_publisher_does_not_start_without_a_readable_volume(live_bridge, monkeypatch):
    monkeypatch.setattr(volume, "_hardware", lambda: None)
    publisher = type(live_bridge.publisher("volume"))(live_bridge)
    assert publisher.start() is False


def test_the_publisher_still_starts_without_a_volume_control(make_bridge, factory, settings,
                                                             receiver, monkeypatch):
    """No wrappers, but the five-second tick still reports every change."""
    monkeypatch.setattr(volume, "_control", lambda: None)
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert "volume" in bridge.capabilities()
