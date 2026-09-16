"""The screenshot: what runs it, what stops it running too often, and what it publishes."""

from conftest import ConsoleAppContainer

from MQTTBridge import screen as screen_module

NODE = "vuuno4kse_005301"
SCREEN = "enigma2/" + NODE + "/screen"
LAST_ERROR = "enigma2/" + NODE + "/last_error"

JPEG = b"\xff\xd8\xff\xe0" + b"picture" * 100


def write_a_picture(path, data=JPEG):
    with open(path, "wb") as handle:
        handle.write(data)


def publisher(bridge, tmp_path):
    found = bridge.publisher("screenshot")
    found.path = str(tmp_path / "mqttbridge.jpg")
    return found


def test_a_capture_runs_grab_through_the_console_container(live_bridge, tmp_path):
    """Never a subprocess: waiting for one blocks the thread that draws the picture."""
    assert publisher(live_bridge, tmp_path).capture(commanded=True) is None
    assert ConsoleAppContainer.instances[-1].commands[-1].startswith("grab ")


def test_the_grab_command_asks_for_both_layers(live_bridge, tmp_path):
    """🔴 `-o` means „the menus only" in this utility, not „output file"."""
    publisher(live_bridge, tmp_path).capture(commanded=True)
    command = ConsoleAppContainer.instances[-1].commands[-1]
    assert " -o " not in command
    assert " -v " not in command
    assert command.endswith(str(tmp_path / "mqttbridge.jpg"))
    assert "-j 80" in command and "-r 720" in command


def test_the_picture_is_published_when_grab_finishes(live_bridge, factory, tmp_path):
    found = publisher(live_bridge, tmp_path)
    found.capture(commanded=True)
    write_a_picture(found.path)
    factory.client.clear()
    ConsoleAppContainer.instances[-1].finish(0)
    entry = factory.client.last(SCREEN)
    assert entry.payload == JPEG
    assert entry.retain is True


def test_the_file_is_removed_after_it_is_published(live_bridge, tmp_path):
    import os

    found = publisher(live_bridge, tmp_path)
    found.capture(commanded=True)
    write_a_picture(found.path)
    ConsoleAppContainer.instances[-1].finish(0)
    assert not os.path.exists(found.path)


def test_a_grab_that_wrote_nothing_publishes_nothing(live_bridge, factory, tmp_path, plugin_log):
    found = publisher(live_bridge, tmp_path)
    found.capture(commanded=True)
    factory.client.clear()
    ConsoleAppContainer.instances[-1].finish(0)
    assert factory.client.all_for(SCREEN) == []
    assert "grab wrote nothing" in plugin_log()


def test_an_empty_file_publishes_nothing(live_bridge, factory, tmp_path):
    found = publisher(live_bridge, tmp_path)
    found.capture(commanded=True)
    write_a_picture(found.path, b"")
    factory.client.clear()
    ConsoleAppContainer.instances[-1].finish(0)
    assert factory.client.all_for(SCREEN) == []


def test_an_oversized_capture_is_refused_rather_than_retained(live_bridge, factory, tmp_path):
    """🔴 `screen` is retained: an enormous one is served to every new subscriber."""
    found = publisher(live_bridge, tmp_path)
    found.capture(commanded=True)
    write_a_picture(found.path, b"x" * (screen_module.MAX_BYTES + 1))
    factory.client.clear()
    ConsoleAppContainer.instances[-1].finish(0)
    assert factory.client.all_for(SCREEN) == []
    assert "over the" in factory.client.last(LAST_ERROR).json()["error"]


def test_a_second_capture_within_five_seconds_is_refused(live_bridge, tmp_path):
    found = publisher(live_bridge, tmp_path)
    found.capture(commanded=True)
    ConsoleAppContainer.instances[-1].finish(0)
    assert "at most one every 5 s" in found.capture(commanded=True)


def test_a_capture_is_allowed_again_after_five_seconds(live_bridge, tmp_path, monkeypatch):
    found = publisher(live_bridge, tmp_path)
    now = [1000.0]
    monkeypatch.setattr(screen_module.time, "time", lambda: now[0])
    found.capture(commanded=True)
    ConsoleAppContainer.instances[-1].finish(0)
    now[0] += 6
    assert found.capture(commanded=True) is None


def test_a_capture_while_one_is_running_is_refused(live_bridge, tmp_path):
    found = publisher(live_bridge, tmp_path)
    found.capture(commanded=True)
    assert "already being taken" in found.capture(commanded=True)


def test_a_zap_takes_a_screenshot_after_the_box_has_settled(live_bridge, factory, receiver,
                                                            tmp_path):
    found = publisher(live_bridge, tmp_path)
    receiver.nav.fire(1)  # evStart
    assert found._debounce.timer.started == (screen_module.DEBOUNCE_MILLISECONDS, True)
    found._debounce.timer.fire()
    assert ConsoleAppContainer.instances[-1].commands


def test_running_through_ten_channels_takes_one_screenshot(live_bridge, receiver, tmp_path):
    """The debounce is the difference between one picture and ten."""
    found = publisher(live_bridge, tmp_path)
    before = len(ConsoleAppContainer.instances)
    for _ in range(10):
        receiver.nav.fire(1)
    found._debounce.timer.fire()
    assert len(ConsoleAppContainer.instances) == before + 1


def test_no_screenshot_on_a_zap_when_the_setting_is_an_interval(live_bridge, receiver, settings,
                                                                tmp_path):
    settings.screenshot.value = "interval"
    found = publisher(live_bridge, tmp_path)
    found._debounce.stopped = False
    receiver.nav.fire(1)
    assert found._debounce.timer is None


def test_the_interval_timer_is_armed_when_the_setting_says_so(make_bridge, factory, settings,
                                                              receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.screenshot.value = "interval"
    settings.screenshot_interval.value = 90
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    assert bridge.publisher("screenshot")._interval.timer.started == (90000, False)


def test_the_interval_never_goes_below_five_seconds(make_bridge, settings, receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.screenshot.value = "interval"
    settings.screenshot_interval.value = 1
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    assert bridge.publisher("screenshot")._interval.timer.started == (5000, False)


def test_screenshots_off_means_no_capability(make_bridge, factory, settings, receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.screenshot.value = "off"
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert "screenshot" not in bridge.capabilities()


def test_no_screenshot_in_standby_unless_asked_for(live_bridge, receiver, tmp_path):
    receiver.enter_standby()
    found = publisher(live_bridge, tmp_path)
    before = len(ConsoleAppContainer.instances)
    assert found.capture() is None
    assert len(ConsoleAppContainer.instances) == before


def test_a_commanded_screenshot_works_in_standby(live_bridge, receiver, tmp_path):
    receiver.enter_standby()
    found = publisher(live_bridge, tmp_path)
    assert found.capture(commanded=True) is None
    assert ConsoleAppContainer.instances[-1].commands


def test_a_grab_that_will_not_start_is_reported(live_bridge, tmp_path, monkeypatch):
    found = publisher(live_bridge, tmp_path)

    class Rejecting(ConsoleAppContainer):
        def __init__(self):
            ConsoleAppContainer.__init__(self)
            self.rejects = True

    monkeypatch.setattr("enigma.eConsoleAppContainer", Rejecting)
    assert "could not be started" in found.capture(commanded=True)


def test_the_last_picture_comes_back_on_a_reconnect(live_bridge, factory, tmp_path):
    found = publisher(live_bridge, tmp_path)
    found.capture(commanded=True)
    write_a_picture(found.path)
    ConsoleAppContainer.instances[-1].finish(0)
    factory.client.clear()
    factory.client.fire_connect()
    assert factory.client.last(SCREEN).payload == JPEG


def test_a_reconnect_does_not_run_grab_again(live_bridge, tmp_path):
    """A reconnect loop that ran `grab` each time would be a receiver on its knees."""
    publisher(live_bridge, tmp_path)
    before = len(ConsoleAppContainer.instances)
    factory_clients = len(ConsoleAppContainer.instances)
    live_bridge.publish_snapshot()
    assert len(ConsoleAppContainer.instances) == before == factory_clients


def test_a_box_without_grab_has_no_screenshots(make_bridge, factory, settings, receiver,
                                               monkeypatch):
    monkeypatch.setattr(screen_module, "grab_binary", lambda: None)
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert "screenshot" not in bridge.capabilities()
