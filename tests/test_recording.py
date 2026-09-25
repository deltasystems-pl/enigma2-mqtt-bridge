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


# A timer the receiver has finished with is not gone: the image keeps it in
# `processed_timers`, OpenWebif lists it, and so does the household's panel. So
# must `timers`, with a word that says what became of it.


def published_timers(bridge, factory):
    factory.client.clear()
    fire_coalesced(bridge, "timers")
    return factory.client.last(TIMERS).json()


def test_ended_timers_are_published_as_ended(live_bridge, factory, receiver):
    receiver.add_timer(begin=1789500000, end=1789501000, name="Pending")
    receiver.add_processed_timer(name="Finished")
    payload = published_timers(live_bridge, factory)
    assert [(entry["name"], entry["state"]) for entry in payload] == [
        ("Finished", "ended"), ("Pending", "waiting"),
    ]


def test_a_disabled_timer_is_published_as_disabled_not_ended(live_bridge, factory, receiver):
    """The image files a disabled timer as `StateEnded`; it is not over."""
    receiver.add_processed_timer(name="Switched off", disabled=True)
    timer = receiver.nav.RecordTimer.processed_timers[0]
    assert timer.state == RecordTimerEntry.StateEnded
    assert [entry["state"] for entry in published_timers(live_bridge, factory)] == ["disabled"]


def test_a_disabled_timer_still_in_the_pending_list_is_disabled(live_bridge, factory, receiver):
    timer = receiver.add_timer()
    timer.disabled = True
    assert [entry["state"] for entry in published_timers(live_bridge, factory)] == ["disabled"]


def test_a_timer_disabled_for_a_conflict_is_published_as_disabled(live_bridge, factory, receiver):
    """What `RecordTimer.record()` does to a conflicting timer while loading the
    file: set `disabled`, then hand it to `addTimerEntry`, which files it with
    the finished ones as `StateEnded`."""
    receiver.add_timer(name="Kept")
    loser = RecordTimerEntry(conftest.ServiceReference(TVN), 1789459200, 1789460700,
                             "Conflicting", "", 0)
    loser.disabled = True
    receiver.nav.RecordTimer.addTimerEntry(loser)
    assert loser in receiver.nav.RecordTimer.processed_timers
    states = {entry["name"]: entry["state"] for entry in published_timers(live_bridge, factory)}
    assert states == {"Kept": "waiting", "Conflicting": "disabled"}


def test_a_timer_that_failed_to_write_is_published_as_failed(live_bridge, factory, receiver):
    """OpenViX marks it `failed` and lets it count on to `StateEnded`."""
    receiver.add_processed_timer(failed=True)
    assert [entry["state"] for entry in published_timers(live_bridge, factory)] == ["failed"]


def test_a_timer_in_state_failed_is_failed_not_waiting(live_bridge, factory, receiver):
    """Images that do use `StateFailed`: the worst word for it is "waiting"."""
    receiver.add_timer(state=RecordTimerEntry.StateFailed)
    assert recording.state_word(RecordTimerEntry.StateFailed) == "failed"
    assert [entry["state"] for entry in published_timers(live_bridge, factory)] == ["failed"]


def test_a_timer_without_a_state_is_unknown(live_bridge, factory, receiver):
    timer = receiver.add_timer()
    del timer.state
    assert [entry["state"] for entry in published_timers(live_bridge, factory)] == ["unknown"]


def test_timers_with_the_same_begin_list_the_pending_one_first(live_bridge, factory, receiver):
    receiver.add_processed_timer(name="Switched off", disabled=True)
    receiver.add_timer(name="Set again")
    payload = published_timers(live_bridge, factory)
    assert [entry["name"] for entry in payload] == ["Set again", "Switched off"]


def test_a_disabled_timer_that_also_failed_is_disabled(live_bridge, factory, receiver):
    """A repeating timer that failed once keeps the flag; switched off, it is
    off - that is what a person has to act on."""
    receiver.add_processed_timer(disabled=True, failed=True)
    assert [entry["state"] for entry in published_timers(live_bridge, factory)] == ["disabled"]


def test_a_requeued_repeating_timer_that_failed_stays_failed(
    live_bridge, factory, receiver, monkeypatch
):
    """The image puts a repeating timer back as waiting for its next day
    without clearing `failed`, and a flagged timer returns before it records.
    "waiting" would promise that next day; the receiver will not record it."""
    monkeypatch.setattr(recording.time, "time", lambda: 1789000000)
    timer = receiver.add_timer(repeated=127)
    timer.failed = True
    assert [entry["state"] for entry in published_timers(live_bridge, factory)] == ["failed"]
    # Documented: `recording` does not read the flag.
    assert recording.read_recording(receiver.session)["next"]["name"] == "Wiadomości"


def test_a_state_nobody_named_is_unknown_not_waiting(live_bridge, factory, receiver):
    receiver.add_timer(state=17)
    assert recording.state_word(17) == "unknown"
    assert [entry["state"] for entry in published_timers(live_bridge, factory)] == ["unknown"]


def test_finished_and_disabled_timers_are_not_recordings(live_bridge, receiver, monkeypatch):
    """`recording` and the guard answer "is the box busy"; a processed timer
    never records, whatever its window says."""
    monkeypatch.setattr(recording.time, "time", lambda: 1789459200 - 120)
    receiver.add_processed_timer(name="Switched off", disabled=True)
    receiver.add_processed_timer(begin=1789459300, end=1789460000, name="Failed", failed=True)
    assert recording.read_recording(receiver.session) == {"active": [], "next": None}
    assert recording.guard(receiver.session) is None


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
    assert recording.add_manual_timer(receiver.session, TVN, 1789500000, 1789501800, "Film") is None
    timer = receiver.nav.RecordTimer.timer_list[0]
    assert (timer.begin, timer.end, timer.name) == (1789500000, 1789501800, "Film")


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


def test_a_timer_whose_window_has_passed_is_not_added(live_bridge, receiver):
    """The image files it straight with the finished ones: kept, never recorded."""
    conftest.record_timer_module.clock = lambda: 1789460700
    error = recording.add_manual_timer(receiver.session, TVN, 1789459200, 1789460700, "Film")
    assert error == "the receiver filed the timer as finished; its window has already passed"
    assert receiver.nav.RecordTimer.timer_list == []
    assert [timer.name for timer in receiver.nav.RecordTimer.processed_timers] == ["Film"]


def test_a_timer_is_deleted_by_service_start_and_end(live_bridge, receiver):
    timer = receiver.add_timer()
    assert recording.delete_timer(receiver.session, TVP1, 1789459200, 1789460700) is None
    assert receiver.nav.RecordTimer.removed == [timer]


def test_deleting_a_timer_that_is_not_there_is_refused(live_bridge, receiver):
    assert "no timer on" in recording.delete_timer(receiver.session, TVP1, 1, 2)


def test_only_the_timer_with_that_exact_end_is_deleted(live_bridge, receiver):
    """A stopped recording keeps its begin and gets a new end; set again, it
    shares service and begin with its old self. Only the triple identifies."""
    stopped = receiver.add_processed_timer(end=1789459800, name="Stopped early")
    again = receiver.add_timer(name="Set again")
    assert recording.delete_timer(receiver.session, TVP1, 1789459200, 1789459800) is None
    assert receiver.nav.RecordTimer.removed == [stopped]
    assert receiver.nav.RecordTimer.timer_list == [again]


def test_an_image_without_a_processed_list_behaves_as_before(live_bridge, receiver):
    del receiver.nav.RecordTimer.processed_timers
    receiver.add_timer()
    assert [entry["state"] for entry in recording.read_timers(receiver.session)] == ["waiting"]
    assert recording.delete_timer(receiver.session, TVP1, 1789459200, 1789460700) is None


def test_an_ended_timer_is_deleted_from_the_processed_list(live_bridge, receiver):
    timer = receiver.add_processed_timer()
    assert recording.delete_timer(receiver.session, TVP1, 1789459200, 1789460700) is None
    assert receiver.nav.RecordTimer.removed == [timer]
    assert receiver.nav.RecordTimer.processed_timers == []


def test_a_disabled_timer_can_be_deleted(live_bridge, receiver):
    timer = receiver.add_processed_timer(disabled=True)
    assert recording.delete_timer(receiver.session, TVP1, 1789459200, 1789460700) is None
    assert receiver.nav.RecordTimer.removed == [timer]


def test_the_pending_copy_of_a_shared_triple_is_deleted_first(live_bridge, receiver):
    """A timer somebody disabled and then set again from the guide: the image
    checks a new timer only against the pending list, so both copies exist with
    one triple. A delete takes the pending one - as it did before finished
    timers were reachable, and as OpenWebif does - and the next delete takes the
    disabled copy."""
    disabled = receiver.add_processed_timer(disabled=True)
    pending = receiver.add_timer()
    assert recording.delete_timer(receiver.session, TVP1, 1789459200, 1789460700) is None
    assert receiver.nav.RecordTimer.removed == [pending]
    assert receiver.nav.RecordTimer.processed_timers == [disabled]
    assert recording.delete_timer(receiver.session, TVP1, 1789459200, 1789460700) is None
    assert receiver.nav.RecordTimer.removed == [pending, disabled]
    assert "no timer on" in recording.delete_timer(receiver.session, TVP1, 1789459200, 1789460700)


def test_a_running_recording_is_still_what_a_shared_triple_deletes(live_bridge, receiver):
    receiver.add_processed_timer()
    running = receiver.add_timer(state=RecordTimerEntry.StateRunning)
    assert recording.delete_timer(receiver.session, TVP1, 1789459200, 1789460700) is None
    assert receiver.nav.RecordTimer.removed == [running]


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
    receiver.epg.events[TVP1] = [Event(4242, 1789500000, 1800, "Named")]
    assert recording.add_event_timer(receiver.session, TVP1, 4242) is None
    assert receiver.nav.RecordTimer.timer_list[0].name == "Named"
