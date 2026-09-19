"""The watchdog observes stalls without doing work on the UI thread."""

import inspect

from MQTTBridge import diagnostics


class FakeThread:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.daemon = False
        self.started = False

    def start(self):
        self.started = True


def publisher(clock, frames=None):
    threads = []

    def factory(**kwargs):
        thread = FakeThread(**kwargs)
        threads.append(thread)
        return thread

    value = diagnostics.LoopMonitor(
        clock=lambda: clock[0], frames=frames or (lambda: {}), thread_factory=factory
    )
    return value, threads


def test_it_starts_a_daemon_with_a_ui_heartbeat():
    clock = [10.0]
    value, threads = publisher(clock)
    assert value.start() is True
    assert threads[0].daemon is True
    assert threads[0].started is True
    assert value._ticker.timer.started == (diagnostics.HEARTBEAT_MILLISECONDS, False)


def test_a_stall_is_reported_at_a_bounded_rate(plugin_log):
    clock = [10.0]
    value, _ = publisher(clock)
    value.start()
    run = value._run
    value._inspect(run, 12.1)
    value._inspect(run, 20.0)
    value._inspect(run, 22.2)
    assert plugin_log().count("event loop stalled") == 2


def test_recovery_is_reported_once_by_the_first_ui_beat(plugin_log):
    clock = [10.0]
    value, _ = publisher(clock)
    value.start()
    run = value._run
    value._inspect(run, 12.1)
    clock[0] = 13.0
    value._beat()
    value._inspect(run, 13.1)
    value._inspect(run, 13.2)
    assert plugin_log().count("event loop resumed") == 1


def test_a_resumed_beat_preserves_a_stall_the_watcher_could_not_run(plugin_log):
    clock = [10.0]
    value, _ = publisher(clock)
    value.start()
    clock[0] = 61.0
    value._beat()
    value._inspect(value._run, 61.1)
    text = plugin_log()
    assert text.count("event loop resumed") == 1
    assert "heartbeat gap 51.0s" in text
    assert "stack" not in text


def test_repeated_resume_gaps_are_rate_limited(plugin_log):
    clock = [10.0]
    value, _ = publisher(clock)
    value.start()
    for now in (13.0, 16.0, 19.0, 23.0):
        clock[0] = now
        value._beat()
    assert plugin_log().count("event loop resumed") == 2


def test_stack_contains_no_absolute_path_source_or_locals(plugin_log):
    secret = "must-not-appear"
    frame = inspect.currentframe()
    clock = [10.0]
    value, _ = publisher(clock, lambda: {value._ui_thread: frame})
    value.start()
    value._inspect(value._run, 12.1)
    output = plugin_log()
    assert "test_diagnostics.py:test_stack_contains_no_absolute_path_source_or_locals" in output
    assert __file__ not in output
    assert secret not in output


def test_stop_invalidates_late_checks_and_does_not_join(plugin_log):
    clock = [10.0]
    value, threads = publisher(clock)
    value.start()
    run = value._run
    value.stop()
    assert not hasattr(threads[0], "join")
    value._inspect(run, 99.0)
    assert "event loop stalled" not in plugin_log()


def test_a_queued_heartbeat_after_stop_is_inert(plugin_log):
    clock = [10.0]
    value, _ = publisher(clock)
    value.start()
    value.stop()
    clock[0] = 61.0
    value._beat()
    assert "event loop resumed" not in plugin_log()


def test_restart_uses_a_new_stop_event_and_invalidates_the_old_run(plugin_log):
    clock = [10.0]
    value, threads = publisher(clock)
    value.start()
    old_run = value._run
    old_event = value._stop_event
    value.stop()
    value.start()
    assert old_event.is_set()
    assert value._stop_event is not old_event
    value._inspect(old_run, 99.0)
    assert len(threads) == 2
    assert "event loop stalled" not in plugin_log()


def test_start_is_idempotent():
    clock = [10.0]
    value, threads = publisher(clock)
    assert value.start() is True
    assert value.start() is True
    assert len(threads) == 1


def test_a_missing_ui_timer_does_not_leave_the_monitor_started(monkeypatch):
    clock = [10.0]
    value, threads = publisher(clock)
    monkeypatch.setattr(value._ticker, "start", lambda _milliseconds: False)
    assert value.start() is False
    assert value._stop_event is None
    assert threads == []
