"""Standby, both ways, and the three ways of switching the box off."""

from MQTTBridge import power

NODE = "vuuno4kse_005301"
POWER = "enigma2/" + NODE + "/power"


def test_a_box_that_is_on_says_so(live_bridge, factory):
    entry = factory.client.last(POWER)
    assert entry.text == "on"
    # Not JSON: the contract says the literal word.
    assert entry.text[0] != '"'
    assert entry.retain is True


def test_a_box_in_standby_says_so_on_connect(make_bridge, factory, settings, receiver):
    receiver.enter_standby()
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert factory.client.last(POWER).text == "standby"


def test_entering_standby_publishes_standby(live_bridge, factory, receiver):
    factory.client.clear()
    receiver.enter_standby()
    assert factory.client.last(POWER).text == "standby"


def test_leaving_standby_publishes_on(live_bridge, factory, receiver):
    receiver.enter_standby()
    factory.client.clear()
    receiver.leave_standby()
    # The publish is deferred by one turn of the main loop, because `onClose`
    # fires while the screen that is closing is still the current one.
    live_bridge.publisher("power")._settle.timer.fire()
    assert factory.client.last(POWER).text == "on"


def test_the_standby_screen_is_only_listened_to_once(live_bridge, receiver):
    screen = receiver.enter_standby()
    live_bridge.publisher("power")._attach_to_standby_screen()
    live_bridge.publisher("power")._attach_to_standby_screen()
    assert len(screen.onClose) == 1


def test_a_second_standby_cycle_is_still_observed(live_bridge, factory, receiver):
    receiver.enter_standby()
    receiver.leave_standby()
    live_bridge.publisher("power")._settle.timer.fire()
    factory.client.clear()
    receiver.enter_standby()
    assert factory.client.last(POWER).text == "standby"


def test_stopping_detaches_from_the_counter(live_bridge, receiver):
    from Components.config import config

    publisher = live_bridge.publisher("power")
    publisher.stop()
    # Only its own listener: the toast holds one on the same counter.
    assert publisher._entered_standby not in config.misc.standbyCounter.notifiers


def test_the_publisher_does_not_start_without_a_standby_counter(live_bridge, monkeypatch):
    from Components.config import config

    monkeypatch.delattr(type(config), "misc", raising=False)
    monkeypatch.setattr(config, "misc", None, raising=False)
    publisher = type(live_bridge.publisher("power"))(live_bridge)
    assert publisher.start() is False


def test_wake_is_idempotent_when_the_box_is_already_awake(receiver):
    assert power.wake() is None


def test_wake_leaves_standby(receiver):
    screen = receiver.enter_standby()
    assert power.wake() is None
    assert screen.power_calls == 1


def test_entering_standby_goes_through_the_notification_queue(receiver):
    from Screens.Standby import Standby
    from Tools.Notifications import Notifications

    assert power.enter_standby() is None
    # The image's five-tuple: (fnc, screen, args, kwargs, id).
    assert Notifications.notifications[-1] == (None, Standby, (), {}, None)


def test_entering_standby_twice_does_nothing_the_second_time(receiver):
    from Tools.Notifications import Notifications

    receiver.enter_standby()
    assert power.enter_standby() is None
    assert Notifications.notifications == []


def test_state_reads_the_screen_not_a_remembered_value(receiver):
    assert power.state() == "on"
    receiver.enter_standby()
    assert power.state() == "standby"


def test_can_quit_refuses_without_a_session(receiver):
    assert "session" in power.can_quit(None)


def test_can_quit_accepts_a_session(receiver):
    assert power.can_quit(receiver.session) is None


def test_quit_opens_the_shutdown_screen_with_its_number(receiver):
    from Screens.Standby import TryQuitMainloop

    assert power.quit_mainloop(receiver.session, power.QUIT_RESTART) is None
    screen, arguments = receiver.session.opened[-1]
    assert screen is TryQuitMainloop
    assert arguments == (3,)


def test_the_shutdown_numbers_are_the_documented_ones():
    assert (power.QUIT_SHUTDOWN, power.QUIT_REBOOT, power.QUIT_RESTART) == (1, 2, 3)
