"""Watching the remote, and pretending to be it."""

from MQTTBridge import keys, remote

NODE = "vuuno4kse_005301"
KEY = "enigma2/" + NODE + "/key"

RED = 398
OK = 352

MAKE, BREAK, REPEAT, LONG = 0, 1, 2, 3


def handler(bridge):
    """The callback the plugin bound into the receiver's action map."""
    return bridge.publisher("keys")._on_key


def press(bridge, code, *flags):
    for flag in flags:
        assert handler(bridge)(code, flag) == 0


def test_a_short_press_is_published_once(live_bridge, factory):
    press(live_bridge, RED, MAKE, BREAK)
    entries = factory.client.all_for(KEY)
    assert len(entries) == 1
    assert entries[0].json() == {"key": "KEY_RED", "press": "short"}


def test_a_key_press_is_never_retained(live_bridge, factory):
    """A retained press re-fires every automation bound to it on every connect."""
    press(live_bridge, RED, MAKE, BREAK)
    assert factory.client.last(KEY).retain is False


def test_a_long_press_is_published_once_as_long(live_bridge, factory):
    press(live_bridge, RED, MAKE, REPEAT, REPEAT, LONG, REPEAT, BREAK)
    entries = factory.client.all_for(KEY)
    assert len(entries) == 1
    assert entries[0].json()["press"] == "long"


def test_a_long_marker_is_terminal_because_openvix_suppresses_its_break(live_bridge,
                                                                        factory):
    press(live_bridge, RED, MAKE, LONG)
    assert factory.client.last(KEY).json()["press"] == "long"
    press(live_bridge, RED, BREAK)
    assert len(factory.client.all_for(KEY)) == 1


def test_a_fresh_make_clears_a_long_markers_tombstone(live_bridge, factory):
    press(live_bridge, RED, MAKE, LONG)
    press(live_bridge, RED, MAKE, BREAK)
    entries = factory.client.all_for(KEY)
    assert [entry.json()["press"] for entry in entries] == ["long", "short"]


def test_repeats_and_duration_recover_a_missing_long_marker(live_bridge, factory,
                                                            monkeypatch):
    """Some physical input drivers deliver repeats and break, but no flag 3."""
    now = [1000.0]
    monkeypatch.setattr(remote.time, "monotonic", lambda: now[0])
    press(live_bridge, RED, MAKE, REPEAT, REPEAT)
    now[0] += 2.0
    press(live_bridge, RED, BREAK)
    entries = factory.client.all_for(KEY)
    assert len(entries) == 1
    assert entries[0].json()["press"] == "long"


def test_an_early_repeat_does_not_turn_a_tap_into_a_long_press(live_bridge, factory,
                                                               monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(remote.time, "monotonic", lambda: now[0])
    press(live_bridge, RED, MAKE, REPEAT)
    now[0] += remote.LONG_PRESS_FALLBACK_SECONDS - 0.01
    press(live_bridge, RED, BREAK)
    assert factory.client.last(KEY).json()["press"] == "short"


def test_duration_without_a_repeat_is_still_a_short_press(live_bridge, factory,
                                                           monkeypatch):
    """Elapsed time alone is not enough evidence to invent a long press."""
    now = [1000.0]
    monkeypatch.setattr(remote.time, "monotonic", lambda: now[0])
    press(live_bridge, RED, MAKE)
    now[0] += 2.0
    press(live_bridge, RED, BREAK)
    assert factory.client.last(KEY).json()["press"] == "short"


def test_repeats_alone_publish_nothing(live_bridge, factory):
    press(live_bridge, RED, MAKE, REPEAT, REPEAT, REPEAT)
    assert factory.client.all_for(KEY) == []


def test_the_handler_always_returns_zero(live_bridge):
    """🔴 Anything else swallows the key and the remote stops working."""
    for flag in (MAKE, BREAK, REPEAT, LONG, 99):
        assert handler(live_bridge)(RED, flag) == 0


def test_the_handler_returns_zero_even_when_it_raises(live_bridge, monkeypatch):
    publisher = live_bridge.publisher("keys")
    monkeypatch.setattr(publisher, "_handle", lambda key, flag: 1 / 0)
    assert publisher._on_key(RED, MAKE) == 0


def test_the_handler_returns_zero_for_nonsense(live_bridge):
    assert handler(live_bridge)("not a key", "not a flag") == 0


def test_two_presses_of_the_same_key_are_two_payloads(live_bridge, factory):
    """A key press is an event, so the same one twice is two of them."""
    press(live_bridge, RED, MAKE, BREAK)
    press(live_bridge, RED, MAKE, BREAK)
    assert len(factory.client.all_for(KEY)) == 2


def test_a_break_without_a_make_is_a_short_press(live_bridge, factory):
    """A button already held when the plugin started still produces one press."""
    press(live_bridge, RED, BREAK)
    assert factory.client.last(KEY).json()["press"] == "short"


def test_an_unknown_key_code_is_published_by_number(live_bridge, factory):
    press(live_bridge, 9999, MAKE, BREAK)
    assert factory.client.last(KEY).json()["key"] == "KEY_9999"


def test_a_key_the_image_knows_and_the_plugin_does_not(live_bridge, factory):
    """`keyids` is merged in, so a remote this plugin never met still names its keys."""
    press(live_bridge, 366, MAKE, BREAK)
    assert factory.client.last(KEY).json()["key"] == "KEY_PVR"


def test_the_plugins_own_name_wins_where_it_has_one(live_bridge, factory):
    """The image lists both KEY_OK and KEY_ENTER for 352; the contract says OK."""
    press(live_bridge, OK, MAKE, BREAK)
    assert factory.client.last(KEY).json()["key"] == "KEY_OK"


def test_the_rate_limit_caps_a_flood(live_bridge, factory):
    for _ in range(40):
        press(live_bridge, RED, MAKE, BREAK)
    assert len(factory.client.all_for(KEY)) == remote.MAX_PUBLISHES_PER_SECOND


def test_the_rate_limit_says_so_once(live_bridge, plugin_log):
    for _ in range(40):
        press(live_bridge, RED, MAKE, BREAK)
    assert plugin_log().count("dropping the rest") == 1


def test_the_rate_limit_lets_go_when_the_second_passes(live_bridge, factory, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(remote.time, "time", lambda: now[0])
    for _ in range(40):
        press(live_bridge, RED, MAKE, BREAK)
    now[0] += 2
    press(live_bridge, RED, MAKE, BREAK)
    assert len(factory.client.all_for(KEY)) == remote.MAX_PUBLISHES_PER_SECOND + 1


def test_publish_keys_off_means_the_remote_is_not_watched(make_bridge, factory, settings,
                                                          receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.publish_keys.value = False
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert "keys" not in bridge.capabilities()
    assert receiver.actions.bound == []


def test_the_binding_is_at_the_lowest_possible_priority(live_bridge, receiver):
    """Every other handler in the receiver is offered the key first."""
    context, priority, _function = receiver.actions.bound[0]
    assert context == ""
    assert priority == remote.BIND_PRIORITY


def test_stopping_unbinds(live_bridge, receiver):
    live_bridge.publisher("keys").stop()
    assert receiver.actions.bound == []


def test_stopping_forgets_an_in_progress_key(live_bridge, factory):
    publisher = live_bridge.publisher("keys")
    press(live_bridge, RED, MAKE, REPEAT)
    publisher.stop()
    assert publisher._held == {}
    assert publisher._finished == set()


def test_a_key_is_injected_as_a_make_and_a_break(live_bridge, receiver):
    assert remote.press("KEY_INFO") is None
    flags = [flag for _device, key, flag in receiver.actions.pressed if key == 358]
    assert flags == [MAKE, BREAK]


def test_a_long_key_is_injected_as_make_long_break(live_bridge, receiver):
    remote.press("KEY_INFO", long=True)
    flags = [flag for _device, key, flag in receiver.actions.pressed if key == 358]
    assert flags == [MAKE, LONG, BREAK]


def test_an_injected_key_claims_a_real_remote(live_bridge, receiver):
    """🔴 A device name no driver registered is a press that arrives nowhere."""
    remote.press("KEY_INFO")
    assert receiver.actions.pressed[0][0] == remote.DEVICE
    assert "remote control" in remote.DEVICE


def test_an_injected_key_reaches_the_plugins_own_listener(live_bridge, factory, receiver):
    """Which is how the box gate proves injection works at all."""
    remote.press("KEY_INFO")
    assert factory.client.last(KEY).json() == {"key": "KEY_INFO", "press": "short"}


def test_an_injected_long_key_is_seen_as_long(live_bridge, factory):
    remote.press("KEY_RED", long=True)
    assert factory.client.last(KEY).json() == {"key": "KEY_RED", "press": "long"}


def test_injection_is_rate_limited_too(live_bridge, receiver):
    """An automation stuck in a loop would otherwise press a key every few
    milliseconds for as long as it ran."""
    for _ in range(40):
        remote.press("KEY_RED")
    presses = [entry for entry in receiver.actions.pressed if entry[1] == 398]
    # Two events — a make and a break — for each of twenty presses.
    assert len(presses) == 2 * remote.MAX_PUBLISHES_PER_SECOND


def test_a_dropped_injection_is_not_an_error(live_bridge):
    """Twenty refusals a second on a retained topic would be their own flood."""
    for _ in range(40):
        assert remote.press("KEY_RED") is None


def test_an_unknown_key_name_is_refused(live_bridge):
    assert "unknown key 'KEY_TELEPORT'" in remote.press("KEY_TELEPORT")


def test_a_key_name_is_not_case_sensitive(live_bridge, receiver):
    assert remote.press("key_info") is None


def test_injection_falls_back_to_the_older_signature(live_bridge, receiver, monkeypatch):
    calls = []

    def two_arguments(*arguments):
        if len(arguments) == 3:
            raise TypeError("this image takes two")
        calls.append(arguments)

    monkeypatch.setattr(receiver.actions, "keyPressed", two_arguments)
    assert remote.press("KEY_INFO") is None
    assert calls == [(358, MAKE), (358, BREAK)]


def test_injection_without_an_action_map_is_refused(live_bridge, monkeypatch):
    monkeypatch.setattr(remote, "action_map", lambda: None)
    assert "no eActionMap" in remote.press("KEY_OK")


def test_the_key_table_answers_both_ways():
    assert keys.code_for("KEY_RED") == 398
    assert keys.name_for(398) == "KEY_RED"
    assert keys.code_for("nonsense") is None
    assert keys.name_for(-1) is None


def test_the_colour_keys_are_the_four_the_contract_names():
    assert keys.COLOUR_KEYS == ("KEY_RED", "KEY_GREEN", "KEY_YELLOW", "KEY_BLUE")
