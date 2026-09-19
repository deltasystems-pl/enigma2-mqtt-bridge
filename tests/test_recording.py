"""Recordings, timers, and the guard that stands in front of the power button."""

import conftest
from conftest import TVN, TVP1, Event, RecordTimerEntry

from MQTTBridge import recording

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
RECORDING = ROOT + "/recording"
TIMERS = ROOT + "/timers"


def fire_coalesced(bridge, name="recording"):
    """Publishing is coalesced by a quarter of a second; this is that quarter."""
    publisher = bridge.publisher(name)
    publisher.soon()
    publisher._coalesce.timer.fire()


# ------------------------------------------------------------------ the topics --


def test_an_idle_box_is_recording_nothing(live_bridge, factory):
    assert factory.client.last(RECORDING).json() == {"active": [], "next": None}


def test_an_idle_box_has_an_empty_timer_list(live_bridge, factory):
    assert factory.client.last(TIMERS).json() == []


def test_a_running_timer_is_an_active_recording(live_bridge, factory, receiver):
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    factory.client.clear()
    fire_coalesced(live_bridge)
    payload = factory.client.last(RECORDING).json()
    assert len(payload["active"]) == 1
    assert payload["active"][0] == {
        "name": "Wiadomości", "sref": TVP1, "begin": 1789459200, "end": 1789460700
    }


def test_a_waiting_timer_is_the_next_recording(live_bridge, factory, receiver, monkeypatch):
    monkeypatch.setattr(recording.time, "time", lambda: 1789000000)
    receiver.add_timer()
    factory.client.clear()
    fire_coalesced(live_bridge)
    payload = factory.client.last(RECORDING).json()
    assert payload["active"] == []
    assert payload["next"]["name"] == "Wiadomości"


def test_the_next_recording_is_the_soonest_of_several(live_bridge, factory, receiver, monkeypatch):
    monkeypatch.setattr(recording.time, "time", lambda: 1789000000)
    receiver.add_timer(begin=1789500000, end=1789501000, name="Later")
    receiver.add_timer(begin=1789100000, end=1789101000, name="Sooner")
    factory.client.clear()
    fire_coalesced(live_bridge)
    assert factory.client.last(RECORDING).json()["next"]["name"] == "Sooner"


def test_a_disabled_timer_is_not_a_next_recording(live_bridge, receiver, monkeypatch):
    monkeypatch.setattr(recording.time, "time", lambda: 1789000000)
    timer = receiver.add_timer()
    timer.disabled = True
    assert recording.read_recording(receiver.session)["next"] is None


def test_a_zap_timer_is_not_a_recording(live_bridge, receiver, monkeypatch):
    """`justplay` means „tune to this", not „write this to the disk"."""
    monkeypatch.setattr(recording.time, "time", lambda: 1789000000)
    receiver.add_timer(justplay=True)
    payload = recording.read_recording(receiver.session)
    assert payload["active"] == [] and payload["next"] is None


def test_the_timer_list_carries_every_documented_field(live_bridge, factory, receiver):
    receiver.add_timer(repeated=127)
    factory.client.clear()
    fire_coalesced(live_bridge, "timers")
    entry = factory.client.last(TIMERS).json()[0]
    assert entry == {
        "name": "Wiadomości", "sref": TVP1, "begin": 1789459200, "end": 1789460700,
        "state": "waiting", "repeated": 127,
    }


def test_the_timer_list_is_a_list_not_an_object(live_bridge, factory, receiver):
    assert isinstance(factory.client.last(TIMERS).json(), list)


def test_the_timer_states_are_words(live_bridge, factory, receiver):
    for state, word in ((0, "waiting"), (1, "prepared"), (2, "running"), (3, "ended")):
        assert recording.state_word(state) == word


def test_the_timer_list_is_in_time_order(live_bridge, factory, receiver):
    receiver.add_timer(begin=1789500000, end=1789501000, name="Later")
    receiver.add_timer(begin=1789100000, end=1789101000, name="Sooner")
    factory.client.clear()
    fire_coalesced(live_bridge, "timers")
    names = [entry["name"] for entry in factory.client.last(TIMERS).json()]
    assert names == ["Sooner", "Later"]


def test_saving_a_timer_publishes_the_list(live_bridge, factory, receiver):
    """The only „the list changed" enigma2 offers is that it wrote the file."""
    factory.client.clear()
    receiver.add_timer()
    receiver.nav.RecordTimer.saveTimer()
    fire_coalesced(live_bridge, "timers")
    assert factory.client.last(TIMERS) is not None


def test_saving_a_timer_still_saves_it(live_bridge, receiver):
    before = receiver.nav.RecordTimer.save_calls
    receiver.nav.RecordTimer.saveTimer()
    assert receiver.nav.RecordTimer.save_calls == before + 1


def test_a_record_event_publishes_too(live_bridge, factory, receiver):
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    factory.client.clear()
    receiver.nav.fire_record(None, 11)
    fire_coalesced(live_bridge)
    assert factory.client.last(RECORDING).json()["active"] != []


def test_publishing_is_coalesced(live_bridge, factory, receiver):
    """One edit calls `saveTimer` several times; a household needs one payload."""
    factory.client.clear()
    receiver.add_timer()
    for _ in range(5):
        receiver.nav.RecordTimer.saveTimer()
    assert factory.client.all_for(TIMERS) == []
    fire_coalesced(live_bridge, "timers")
    assert len(factory.client.all_for(TIMERS)) == 1


def test_stopping_puts_savetimer_back(live_bridge, receiver):
    wrapped = receiver.nav.RecordTimer.saveTimer
    live_bridge.publisher("timers").stop()
    # The sibling is still listening, so the wrapper stays until it stops too.
    assert receiver.nav.RecordTimer.saveTimer is wrapped
    live_bridge.publisher("recording").stop()
    assert receiver.nav.RecordTimer.saveTimer is not wrapped


def test_stopping_detaches_from_the_record_event(live_bridge, receiver):
    before = len(receiver.nav.record_event)
    live_bridge.publisher("recording").stop()
    assert len(receiver.nav.record_event) == before - 1


# ------------------------------------------------------------------- the guard --


def test_the_guard_lets_an_idle_box_through(live_bridge, receiver):
    assert recording.guard(receiver.session) is None


def test_the_guard_refuses_while_recording(live_bridge, receiver):
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    assert recording.guard(receiver.session) == "the receiver is recording"


def test_the_guard_refuses_a_recording_that_is_about_to_start(live_bridge, receiver, monkeypatch):
    monkeypatch.setattr(recording.time, "time", lambda: 1789459200 - 120)
    receiver.add_timer()
    assert "starts in 120 s" in recording.guard(receiver.session)


def test_the_guard_allows_a_recording_that_is_further_off(live_bridge, receiver, monkeypatch):
    monkeypatch.setattr(recording.time, "time", lambda: 1789459200 - 601)
    receiver.add_timer()
    assert recording.guard(receiver.session) is None


def test_the_guard_refuses_when_it_cannot_tell(live_bridge, receiver):
    """🔴 „I could not read it" is not „nothing is recording"."""
    receiver.nav.RecordTimer.isRecording = None
    assert "refusing to risk it" in recording.guard(receiver.session)


def test_the_guard_window_is_ten_minutes():
    assert recording.GUARD_WINDOW_SECONDS == 600


# ------------------------------------------------------------------- commands --


def test_a_timer_is_built_from_an_event(live_bridge, receiver):
    assert recording.add_event_timer(receiver.session, TVP1, 27431) is None
    timer = receiver.nav.RecordTimer.timer_list[0]
    assert timer.name == "Wiadomości"
    assert timer.begin == 1789459200
    assert timer.end == 1789459200 + 1500
    assert timer.eit == 27431


def test_a_timer_from_an_event_inherits_the_recording_margins(live_bridge, receiver):
    conftest.record_timer_module.margin_before = 2
    conftest.record_timer_module.margin_after = 5
    recording.add_event_timer(receiver.session, TVP1, 27431)
    timer = receiver.nav.RecordTimer.timer_list[0]
    assert timer.begin == 1789459200 - 120
    assert timer.end == 1789459200 + 1500 + 300


def test_an_unknown_event_is_refused(live_bridge, receiver):
    assert "no event 999" in recording.add_event_timer(receiver.session, TVP1, 999)


def test_a_manual_timer_is_built_from_its_window(live_bridge, receiver):
    assert recording.add_manual_timer(receiver.session, TVN, 100, 200, "Film") is None
    timer = receiver.nav.RecordTimer.timer_list[0]
    assert (timer.begin, timer.end, timer.name) == (100, 200, "Film")


def test_a_manual_timer_that_ends_before_it_begins_is_refused(live_bridge, receiver):
    assert "ends before" in recording.add_manual_timer(receiver.session, TVN, 200, 100, "x")


def test_a_manual_timer_with_nonsense_times_is_refused(live_bridge, receiver):
    assert "epoch seconds" in recording.add_manual_timer(receiver.session, TVN, "soon", "later", "")


def test_a_conflicting_timer_names_the_conflict(live_bridge, receiver):
    other = receiver.add_timer(name="Something else")
    receiver.nav.RecordTimer.conflicts = [other]
    error = recording.add_manual_timer(receiver.session, TVN, 100, 200, "Film")
    assert "conflicts with Something else" in error


def test_a_timer_the_receiver_quietly_dropped_is_reported(live_bridge, receiver):
    """🔴 `record()` answers None both for „accepted" and for „already had one"."""
    receiver.nav.RecordTimer.swallow = True
    error = recording.add_manual_timer(receiver.session, TVN, 100, 200, "Film")
    assert "did not keep the timer" in error


def test_a_timer_is_deleted_by_service_start_and_end(live_bridge, receiver):
    timer = receiver.add_timer()
    assert recording.delete_timer(receiver.session, TVP1, 1789459200, 1789460700) is None
    assert receiver.nav.RecordTimer.removed == [timer]


def test_deleting_a_timer_that_is_not_there_is_refused(live_bridge, receiver):
    assert "no timer on" in recording.delete_timer(receiver.session, TVP1, 1, 2)


def test_a_timer_is_matched_whatever_the_reference_is_spelled_like(live_bridge, receiver):
    receiver.add_timer()
    assert recording.delete_timer(
        receiver.session, TVP1 + ":TVP 1 HD", 1789459200, 1789460700
    ) is None


def test_an_instant_recording_records_what_is_playing(live_bridge, receiver, monkeypatch):
    monkeypatch.setattr(recording.time, "time", lambda: 1789460000)
    assert recording.start_instant_recording(receiver.session) is None
    timer = receiver.nav.RecordTimer.timer_list[0]
    assert str(timer.service_ref) == TVP1
    assert timer.begin == 1789460000
    assert timer.end == 1789460000 + recording.INSTANT_RECORD_SECONDS


def test_an_instant_recording_is_named_after_the_programme(live_bridge, receiver):
    recording.start_instant_recording(receiver.session)
    assert receiver.nav.RecordTimer.timer_list[0].name == "Wiadomości"


def test_an_instant_recording_of_a_channel_with_no_epg_still_has_a_name(live_bridge, receiver):
    receiver.info.events = []
    recording.start_instant_recording(receiver.session)
    assert receiver.nav.RecordTimer.timer_list[0].name == "instant record"


def test_an_instant_recording_needs_something_to_be_playing(live_bridge, receiver):
    receiver.nav.sref = ""
    assert recording.start_instant_recording(receiver.session) == "nothing is playing"


def test_stopping_removes_the_running_recording(live_bridge, receiver):
    timer = receiver.add_timer(state=RecordTimerEntry.StateRunning)
    assert recording.stop_instant_recording(receiver.session) is None
    assert receiver.nav.RecordTimer.removed == [timer]


def test_stopping_a_recording_of_another_channel_is_refused(live_bridge, receiver):
    receiver.add_timer(sref=TVN, state=RecordTimerEntry.StateRunning)
    assert "nothing is being recorded" in recording.stop_instant_recording(receiver.session)


def test_stopping_when_nothing_is_recording_is_refused(live_bridge, receiver):
    assert "nothing is being recorded" in recording.stop_instant_recording(receiver.session)


def test_an_event_timer_needs_an_epg_cache(live_bridge, receiver, monkeypatch):
    monkeypatch.setattr(recording, "_epg_cache", lambda: None)
    assert "no EPG cache" in recording.add_event_timer(receiver.session, TVP1, 27431)


def test_reading_timers_from_a_box_with_no_record_timer(receiver):
    receiver.nav.RecordTimer = None
    assert recording.read_timers(receiver.session) == []
    assert recording.read_recording(receiver.session) == {"active": [], "next": None}


def test_the_epg_cache_finds_an_event_by_its_id(live_bridge, receiver):
    receiver.epg.events[TVP1] = [Event(4242, 10, 20, "Named")]
    assert recording.add_event_timer(receiver.session, TVP1, 4242) is None
    assert receiver.nav.RecordTimer.timer_list[0].name == "Named"
