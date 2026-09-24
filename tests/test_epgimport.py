"""Asking the image's EPG importer for an import, and following every import it runs.

The importer here is `conftest.install_epg_importer`: a module in `sys.modules`
where enigma2's plugin loader would have left it, shaped on the bytecode of the
receiver this was measured on. Its `isImportRunning()` is the image's own test
(`source is not None`), and `finish()` does what the end of the image's download
chain does - clears the running flag, and calls the completion that records
`lastImportResult` - in one turn, which is why one poll afterwards is enough.
"""

import builtins
import importlib
import json
import sys
import time

import conftest
import pytest
from Components.config import config
from conftest import (
    EPG_IMPORTER_MODULE,
    EPGCache,
    FakeEpgSource,
    RecordTimerEntry,
    install_epg_importer,
    settle,
)

from MQTTBridge import epgimport
from MQTTBridge.origin import PAGE

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
TOPIC = ROOT + "/epg_import"
INFO = ROOT + "/info"
LAST_ERROR = ROOT + "/last_error"
DEVICE = "homeassistant/device/" + NODE + "/config"


# ---------------------------------------------------------------------- helpers --


@pytest.fixture
def importer(monkeypatch):
    return install_epg_importer(monkeypatch)


@pytest.fixture
def epg_bridge(make_bridge, factory, settings, receiver):
    """A connected bridge on a working receiver; the importer is whatever the test installed."""

    def build(allowed=True):
        settings.host.value = "10.0.0.5"
        settings.node_id.value = NODE
        settings.friendly_name.value = "Living room receiver"
        settings.epg_import_allowed.value = allowed
        bridge = make_bridge(session=receiver.session)
        bridge.start()
        factory.client.fire_connect()
        return settle(bridge)

    return build


def send(factory, payload=b"PRESS", retain=False):
    factory.client.fire_message(ROOT + "/cmd/epg_import", payload, retain=retain)


def error(factory):
    entry = factory.client.last(LAST_ERROR)
    return None if entry is None or entry.text == "" else entry.json()["error"]


def state(factory):
    return factory.client.last(TOPIC).json()


def tick(bridge):
    bridge.publisher("epg_import")._poll.timer.fire()


def components(factory):
    return factory.client.last(DEVICE).json()["cmps"]


# ------------------------------------------------------------------ capability --


def test_no_capability_when_the_importer_is_not_loaded(epg_bridge, factory):
    bridge = epg_bridge()
    assert bridge.publisher("epg_import") is None
    assert "epg_import" not in factory.client.last(INFO).json()["capabilities"]
    assert factory.client.last(TOPIC) is None


def test_the_capability_and_the_topic_appear_together(importer, epg_bridge, factory):
    bridge = epg_bridge()
    assert "epg_import" in bridge.capabilities()
    assert "epg_import" in factory.client.last(INFO).json()["capabilities"]
    assert state(factory) == {
        "state": "idle", "started": None, "finished": None, "events": None, "error": None,
    }


@pytest.mark.parametrize("missing", [
    "epgimport", "isImportRunning", "sources", "startImport", "EPGConfig",
    "loadUserSettings", "enumSources", "CONFIG_PATH", "lastImportResult",
])
def test_no_capability_when_any_resolved_name_is_missing(
    importer, epg_bridge, factory, missing
):
    if missing in ("isImportRunning", "sources"):
        # An object without the name, rather than a deleted attribute on the fake.
        class Bare:
            pass

        bare = Bare()
        if missing == "sources":
            bare.isImportRunning = lambda: False
        else:
            bare.sources = []
        importer.epgimport = bare
    elif missing in ("loadUserSettings", "enumSources"):
        delattr(importer.EPGConfig, missing)
    else:
        delattr(importer, missing)
    bridge = epg_bridge()
    assert "epg_import" not in bridge.capabilities()
    assert factory.client.last(TOPIC) is None


def test_no_capability_when_the_cache_cannot_take_imported_events(
    monkeypatch, epg_bridge, factory, plugin_log
):
    """🔴 Without them the importer writes a file and ends by restarting the GUI."""
    install_epg_importer(monkeypatch, import_events=False, import_event=False)
    bridge = epg_bridge()
    assert "epg_import" not in bridge.capabilities()
    assert "would end by restarting the user interface" in plugin_log()


def test_the_older_single_event_method_is_enough(monkeypatch, epg_bridge):
    install_epg_importer(monkeypatch, import_events=False, import_event=True)
    assert "epg_import" in epg_bridge().capabilities()


def test_the_plugin_never_imports_the_importer_itself(epg_bridge, factory, monkeypatch):
    """🔴 A second import would build a second importer and a second scheduler."""
    attempts = []

    class Watcher:
        def find_spec(self, name, path=None, target=None):
            if name.startswith("Plugins.Extensions.EPGImport"):
                attempts.append(("find_spec", name))
            return None

    real_import = builtins.__import__
    real_import_module = importlib.import_module

    def spying_import(name, *arguments, **keywords):
        if "EPGImport" in name:
            attempts.append(("__import__", name))
        return real_import(name, *arguments, **keywords)

    def spying_import_module(name, *arguments, **keywords):
        if "EPGImport" in name:
            attempts.append(("import_module", name))
        return real_import_module(name, *arguments, **keywords)

    monkeypatch.setattr(sys, "meta_path", [Watcher()] + sys.meta_path)
    monkeypatch.setattr(builtins, "__import__", spying_import)
    monkeypatch.setattr(importlib, "import_module", spying_import_module)

    bridge = epg_bridge()
    send(factory)
    assert "epg_import" not in bridge.capabilities()
    assert attempts == []

    # And with it loaded: the object used is the very one the loader left.
    module = install_epg_importer(monkeypatch)
    bridge = epg_bridge()
    assert "epg_import" in bridge.capabilities()
    send(factory)
    assert attempts == []
    assert module.started == 1
    assert sys.modules[EPG_IMPORTER_MODULE] is module


# ---------------------------------------------------------------------- guards --


def test_refused_without_the_permission(importer, epg_bridge, factory):
    epg_bridge(allowed=False)
    send(factory)
    assert error(factory) == epgimport.PERMISSION
    assert importer.started == 0


def test_the_permission_is_refused_before_a_missing_importer(epg_bridge, factory, settings):
    """The documented order: permission first, then „no importer"."""
    epg_bridge(allowed=False)
    send(factory)
    assert error(factory) == epgimport.PERMISSION
    settings.epg_import_allowed.value = True
    send(factory)
    assert error(factory) == epgimport.NOT_RESOLVED


def test_the_openwebif_page_needs_no_permission(importer, epg_bridge, factory):
    bridge = epg_bridge(allowed=False)
    assert bridge.run_command("epg_import", "", PAGE) is None
    assert importer.started == 1
    assert error(factory) is None


def test_mqtt_is_still_refused_after_the_page_ran_one(importer, epg_bridge, factory):
    """The origin is an argument, never a mode."""
    bridge = epg_bridge(allowed=False)
    bridge.run_command("epg_import", "", PAGE)
    importer.epgimport.finish(100)
    tick(bridge)
    send(factory)
    assert error(factory) == epgimport.PERMISSION
    assert importer.started == 1


def test_refused_while_running_whoever_started_it(importer, epg_bridge, factory):
    epg_bridge()
    # The image's schedule, or the importer's own screen.
    importer.epgimport.source = object()
    send(factory)
    assert error(factory) == epgimport.ALREADY_RUNNING
    assert importer.started == 0


def test_refused_while_recording(importer, epg_bridge, factory, receiver):
    epg_bridge()
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    send(factory)
    assert error(factory) == "the receiver is recording"
    assert importer.started == 0


def test_refused_with_a_timer_due_within_ten_minutes(importer, epg_bridge, factory, receiver):
    """✅ The whole recording guard: the final save holds the loop that starts recordings."""
    epg_bridge()
    receiver.add_timer(begin=int(time.time()) + 60, end=int(time.time()) + 600)
    send(factory)
    assert "a recording starts in" in error(factory)
    assert importer.started == 0


def test_refused_when_the_image_will_not_say_whether_it_records(
    importer, epg_bridge, factory, receiver, monkeypatch
):
    epg_bridge()
    monkeypatch.setattr(receiver.nav, "RecordTimer", None)
    send(factory)
    assert "will not say whether it is recording" in error(factory)
    assert importer.started == 0


def test_refused_within_ten_minutes_of_the_image_s_own_import(importer, epg_bridge, factory):
    epg_bridge()
    importer.autoStartTimer.wake = int(time.time()) + 300
    send(factory)
    assert error(factory).startswith("EPG-Importer's own scheduled import starts in")
    assert importer.started == 0


def test_not_refused_eleven_minutes_before_the_image_s_own_import(
    importer, epg_bridge, factory
):
    epg_bridge()
    importer.autoStartTimer.wake = int(time.time()) + 660
    send(factory)
    assert error(factory) is None
    assert importer.started == 1


def test_a_scheduled_time_already_past_today_counts_from_tomorrow(
    importer, epg_bridge, factory
):
    """Decision 1: `getWakeTime()` answers today's clock time, even once it has passed."""
    epg_bridge()
    importer.autoStartTimer.wake = int(time.time()) + 300 - 24 * 3600
    send(factory)
    assert error(factory).startswith("EPG-Importer's own scheduled import starts in")
    assert importer.started == 0


def test_a_schedule_switched_off_does_not_refuse(importer, epg_bridge, factory):
    epg_bridge()
    importer.autoStartTimer.wake = int(time.time()) + 300
    config.plugins.epgimport.enabled.value = False
    send(factory)
    assert error(factory) is None
    assert importer.started == 1


@pytest.mark.parametrize("wake", [RuntimeError("no clock"), "soon", None])
def test_a_scheduled_time_that_cannot_be_read_refuses(importer, epg_bridge, factory, wake):
    """Decision 2: „I could not tell when it runs" is not „it does not run soon"."""
    epg_bridge()
    importer.autoStartTimer.wake = wake
    send(factory)
    assert error(factory) == epgimport.UNKNOWN_SCHEDULE
    assert importer.started == 0


def test_refused_with_no_sources_selected(importer, epg_bridge, factory):
    epg_bridge()
    importer.EPGConfig.selection["sources"] = []
    send(factory)
    assert error(factory) == epgimport.NO_SOURCES
    assert importer.started == 0


def test_an_importer_that_will_not_say_whether_it_runs_refuses(importer, epg_bridge, factory):
    """Decision 3, first half."""
    epg_bridge()
    importer.epgimport.raises = True
    send(factory)
    assert error(factory) == epgimport.UNKNOWN_RUNNING
    assert importer.started == 0


# -------------------------------------------------------------------- starting --


def test_the_payload_is_ignored(importer, epg_bridge, factory):
    """🔴 Nothing from the broker reaches the importer."""
    epg_bridge()
    send(factory, b'{"sources": ["http://attacker.example/evil.xml"]}')
    assert error(factory) is None
    assert importer.epgimport.began == [["Deutschland - Basis", "Polska - Podstawowy"]]


def test_the_sources_are_handed_over_reversed_as_the_manual_button_does(
    importer, epg_bridge, factory
):
    """The importer `pop()`s, so the first selected source must be last in the list."""
    epg_bridge()
    send(factory)
    assert importer.epgimport.began == [["Deutschland - Basis", "Polska - Podstawowy"]]
    assert importer.epgimport.source.description == "Polska - Podstawowy"


def test_a_retained_command_is_discarded(importer, epg_bridge, factory):
    epg_bridge()
    send(factory, retain=True)
    assert importer.started == 0


def test_the_channel_cache_is_kept_when_nothing_changed(importer, epg_bridge, factory):
    """Decision 7: the scheduler's own test, and its state is only ever read."""
    epg_bridge()
    send(factory)
    assert importer.EPGConfig.channelCache == {"cached": True}
    assert importer.autoStartTimer.prev_onlybouquet is False
    assert importer.autoStartTimer.prev_multibouquet is True


@pytest.mark.parametrize("change", ["onlybouquet", "multibouquet"])
def test_the_channel_cache_is_reset_when_the_bouquet_settings_changed(
    importer, epg_bridge, factory, change
):
    epg_bridge()
    if change == "onlybouquet":
        config.plugins.epgimport.import_onlybouquet.value = True
    else:
        config.usage.multibouquet.value = False
    send(factory)
    assert importer.EPGConfig.channelCache == {}
    # Read, never written: the scheduler still makes its own decision next time.
    assert importer.autoStartTimer.prev_onlybouquet is False
    assert importer.autoStartTimer.prev_multibouquet is True


def test_without_a_scheduler_the_channel_cache_is_always_reset(
    monkeypatch, epg_bridge, factory
):
    module = install_epg_importer(monkeypatch, scheduler=False)
    epg_bridge()
    send(factory)
    assert module.EPGConfig.channelCache == {}
    assert module.started == 1


@pytest.mark.parametrize("where", ["startImport", "loadUserSettings"])
def test_a_start_that_raises_is_failed_on_the_topic_and_on_last_error(
    importer, epg_bridge, factory, where
):
    """Cross-half decision: the same sentence on both, so the button raises the real reason."""
    epg_bridge()

    def broken(*_arguments, **_keywords):
        raise OSError("disk gone")

    if where == "startImport":
        importer.startImport = broken
    else:
        importer.EPGConfig.loadUserSettings = broken
    send(factory)
    payload = state(factory)
    assert payload["state"] == "failed"
    assert payload["error"] == "EPG-Importer could not start: OSError"
    assert error(factory) == payload["error"]
    assert "disk gone" not in payload["error"]


# -------------------------------------------------------------------- the topic --


def test_idle_running_done_with_timestamps(importer, epg_bridge, factory):
    bridge = epg_bridge()
    before = int(time.time())
    send(factory)
    running = state(factory)
    assert running["state"] == "running"
    assert before <= running["started"] <= int(time.time())
    assert running["finished"] is None and running["events"] is None
    assert running["error"] is None
    assert error(factory) is None

    tick(bridge)
    assert state(factory)["state"] == "running"

    importer.epgimport.finish(120074)
    tick(bridge)
    done = state(factory)
    assert done["state"] == "done"
    assert done["started"] == running["started"]
    assert done["finished"] == int(importer.lastImportResult[0])
    assert done["events"] == 120074
    assert done["error"] is None


def test_the_running_poll_is_two_seconds_and_the_idle_poll_a_minute(
    importer, epg_bridge, factory
):
    bridge = epg_bridge()
    timer = bridge.publisher("epg_import")._poll.timer
    assert timer.started == (60000, False)
    send(factory)
    assert timer.started == (2000, False)
    importer.epgimport.finish(10)
    tick(bridge)
    assert timer.started == (60000, False)


def test_no_events_is_failed_with_its_sentence(importer, epg_bridge, factory):
    bridge = epg_bridge()
    send(factory)
    importer.epgimport.finish(0)
    tick(bridge)
    payload = state(factory)
    assert payload["state"] == "failed"
    assert payload["events"] == 0
    assert payload["error"] == (
        "EPG-Importer finished without importing any events; its sources may be unreachable"
    )


def test_an_import_that_never_ran_is_failed(importer, epg_bridge, factory):
    bridge = epg_bridge()
    send(factory)
    # The running flag cleared without the completion ever recording a result.
    importer.epgimport.source = None
    tick(bridge)
    assert state(factory)["state"] == "failed"
    assert state(factory)["error"] == epgimport.DID_NOT_RUN


def test_an_import_that_never_ran_is_failed_after_an_earlier_one(importer, epg_bridge, factory):
    """The earlier run's result is still on the record; it is not this run's."""
    bridge = epg_bridge()
    send(factory)
    importer.epgimport.finish(10)
    tick(bridge)
    assert state(factory)["state"] == "done"

    send(factory)
    importer.epgimport.source = None
    tick(bridge)
    assert state(factory)["state"] == "failed"
    assert state(factory)["error"] == epgimport.DID_NOT_RUN
    assert state(factory)["events"] is None


def test_the_watchdog_fires_at_thirty_minutes_and_keeps_the_running_guard(
    importer, epg_bridge, factory
):
    assert epgimport.WATCHDOG_SECONDS == 30 * 60
    bridge = epg_bridge()
    publisher = bridge.publisher("epg_import")
    send(factory)
    assert publisher._deadline - time.monotonic() > 30 * 60 - 5

    publisher._deadline = time.monotonic() + 60
    tick(bridge)
    assert state(factory)["state"] == "running"

    publisher._deadline = time.monotonic() - 1
    tick(bridge)
    assert state(factory)["state"] == "failed"
    assert state(factory)["error"] == "EPG-Importer has not finished after 30 minutes"
    # Still polling every two seconds, and still refusing a second start.
    assert publisher._poll.timer.started == (2000, False)
    send(factory)
    assert error(factory) == epgimport.ALREADY_RUNNING
    assert importer.started == 1


def test_the_real_result_replaces_the_watchdog_s_failure(importer, epg_bridge, factory):
    """Decision 5."""
    bridge = epg_bridge()
    publisher = bridge.publisher("epg_import")
    send(factory)
    publisher._deadline = time.monotonic() - 1
    tick(bridge)
    assert state(factory)["state"] == "failed"
    importer.epgimport.finish(500)
    tick(bridge)
    assert state(factory)["state"] == "done"
    assert state(factory)["events"] == 500
    assert state(factory)["error"] is None


def test_an_import_somebody_else_started_is_followed(importer, epg_bridge, factory):
    """✅ The topic follows every import, so „already running" is never unexplained."""
    bridge = epg_bridge()
    importer.epgimport.sources = [FakeEpgSource("Polska - Podstawowy")]
    importer.startImport()  # the image's schedule
    before = int(time.time())
    tick(bridge)
    running = state(factory)
    assert running["state"] == "running"
    assert before <= running["started"] <= int(time.time())
    assert bridge.publisher("epg_import")._poll.timer.started == (2000, False)

    importer.epgimport.finish(77)
    tick(bridge)
    assert state(factory)["state"] == "done"
    assert state(factory)["events"] == 77


def test_an_import_that_began_and_ended_between_two_idle_polls_is_reported(
    importer, epg_bridge, factory
):
    """Decision 4: seen only by its result, so `started` is null."""
    bridge = epg_bridge()
    importer.epgimport.sources = [FakeEpgSource("Polska - Podstawowy")]
    importer.startImport()
    importer.epgimport.finish(42)
    tick(bridge)
    payload = state(factory)
    assert payload["state"] == "done"
    assert payload["started"] is None
    assert payload["events"] == 42
    assert payload["finished"] == int(importer.lastImportResult[0])


def test_an_idle_poll_with_nothing_new_publishes_nothing(importer, epg_bridge, factory):
    bridge = epg_bridge()
    count = len(factory.client.all_for(TOPIC))
    tick(bridge)
    tick(bridge)
    assert len(factory.client.all_for(TOPIC)) == count


def test_the_start_is_seeded_from_the_importer_s_own_record(monkeypatch, epg_bridge, factory):
    module = install_epg_importer(monkeypatch)
    config.plugins.extra_epgimport.last_import.value = "Tue Sep 22 08:08:12 2026, 120074"
    epg_bridge()
    payload = state(factory)
    assert payload["state"] == "idle"
    assert payload["events"] == 120074
    assert payload["finished"] == int(time.mktime((2026, 9, 22, 8, 8, 12, 0, 0, -1)))
    assert payload["started"] is None
    assert module.started == 0


def test_an_import_already_running_at_start_is_reported_running(
    monkeypatch, epg_bridge, factory
):
    module = install_epg_importer(monkeypatch)
    module.epgimport.source = object()
    epg_bridge()
    assert state(factory)["state"] == "running"


@pytest.mark.parametrize("text", ["none", "", "Tue Sep 22 08:08:12 2026", "Xyz 1 2 3 4, 5"])
def test_an_unreadable_record_seeds_nothing(text):
    assert epgimport.parse_last_import(text) == (None, None)


def test_the_record_is_read_whatever_the_locale():
    """`asctime` writes English; `strptime` would read the image's locale."""
    assert epgimport.parse_last_import("Wed Sep  2 23:59:01 2026, 3") == (
        int(time.mktime((2026, 9, 2, 23, 59, 1, 0, 0, -1))), 3,
    )


# --------------------------------------------------------------------- the grid --


def test_the_grid_is_regenerated_after_done_and_not_after_failed(
    importer, epg_bridge, factory, monkeypatch
):
    bridge = epg_bridge()
    grid = bridge.publisher("epg_grid")
    calls = []
    monkeypatch.setattr(grid, "regenerate", lambda: calls.append(1))

    send(factory)
    importer.epgimport.finish(0)
    tick(bridge)
    assert calls == []

    send(factory)
    importer.epgimport.finish(10)
    tick(bridge)
    assert calls == [1]


def test_an_unchanged_grid_publishes_nothing(importer, epg_bridge, factory):
    """ADR-0006: a rebuild that finds the same television publishes nothing."""
    bridge = epg_bridge()
    grids = [topic for topic in factory.client.topics() if "/epg_grid/" in topic]
    assert grids
    before = len([t for t in factory.client.topics() if "/epg_grid/" in t])
    send(factory)
    importer.epgimport.finish(10)
    tick(bridge)
    settle(bridge)
    assert state(factory)["state"] == "done"
    assert len([t for t in factory.client.topics() if "/epg_grid/" in t]) == before


# ------------------------------------------------------ the power commands (✅) --


@pytest.mark.parametrize("command", ["deep_standby", "reboot", "restart_gui"])
def test_the_power_commands_are_refused_while_an_import_runs(
    importer, epg_bridge, factory, settings, command
):
    epg_bridge()
    settings.deep_standby_allowed.value = True
    importer.epgimport.source = object()
    factory.client.fire_message(ROOT + "/cmd/" + command, b"PRESS")
    assert error(factory) == "an EPG import is running"


def test_the_page_s_restart_is_refused_while_an_import_runs(importer, epg_bridge):
    bridge = epg_bridge()
    importer.epgimport.source = object()
    assert bridge.run_command("restart_gui", "", PAGE) == "an EPG import is running"


def test_a_broken_importer_does_not_block_the_power_commands(
    importer, epg_bridge, factory, receiver
):
    """Decision 3, second half: an importer that will not answer is not a running one."""
    epg_bridge()
    importer.epgimport.raises = True
    factory.client.fire_message(ROOT + "/cmd/restart_gui", b"PRESS")
    assert error(factory) is None
    assert receiver.session.opened


def test_without_an_importer_the_power_commands_are_unaffected(epg_bridge, factory, receiver):
    epg_bridge()
    factory.client.fire_message(ROOT + "/cmd/restart_gui", b"PRESS")
    assert error(factory) is None


# --------------------------------------------------------- permission and info --


def test_the_permission_is_published_read_only(connected_bridge, factory, settings):
    assert factory.client.last(INFO).json()["settings"]["epg_import_allowed"] is False
    assert "epg_import_allowed" not in connected_bridge.remote_settings()
    factory.client.fire_message(
        ROOT + "/cmd/config",
        b'{"publish_keys":true,"screenshot":"on_zap","screenshot_interval":60,'
        b'"epg_import_allowed":true}',
    )
    assert error(factory) == "the config object contains unknown settings"
    assert settings.epg_import_allowed.value is False


# ------------------------------------------------------------------- discovery --


def test_the_sensor_follows_the_capability(epg_bridge, factory, monkeypatch):
    epg_bridge()
    assert "epg_import" not in components(factory)


def test_the_sensor_and_its_templates(importer, epg_bridge, factory):
    bridge = epg_bridge()
    sensor = components(factory)["epg_import"]
    assert sensor["p"] == "sensor"
    assert sensor["stat_t"] == TOPIC
    assert sensor["ent_cat"] == "diagnostic"

    send(factory)
    importer.epgimport.finish(12)
    tick(bridge)
    payload = state(factory)
    assert conftest.render_value_template(sensor["val_tpl"], payload) == "done"
    attributes = json.loads(conftest.render_value_template(sensor["json_attr_tpl"], payload))
    assert attributes == {
        "started": payload["started"], "finished": payload["finished"], "events": 12,
        "error": None,
    }
    absent = {key: value for key, value in payload.items() if key != "state"}
    assert conftest.render_value_template(sensor["val_tpl"], absent) == "None"


def test_the_button_follows_the_permission(importer, epg_bridge, factory, settings):
    epg_bridge(allowed=False)
    assert "epg_import_start" not in components(factory)
    settings.epg_import_allowed.value = True
    factory.client.fire_connect()
    button = components(factory)["epg_import_start"]
    assert button["p"] == "button"
    assert button["cmd_t"] == ROOT + "/cmd/epg_import"


def test_no_button_without_the_capability_even_with_the_permission(epg_bridge, factory):
    epg_bridge(allowed=True)
    assert "epg_import_start" not in components(factory)


def test_the_cache_stub_starts_without_either_import_method():
    """The default receiver is the one the capability must not be claimed on."""
    cache = EPGCache.getInstance()
    assert not hasattr(cache, "importEvents")
    assert not hasattr(cache, "importEvent")


# -------------------------------------------- the power block lapses (✅ review) --


def _start_that_raises_part_way(importer):
    """What the image's `nextImport` does: mark itself running, then fail to fetch."""

    def start():
        importer.epgimport.source = object()
        raise OSError("fetch failed")

    importer.startImport = start


def test_the_power_block_lapses_after_our_own_start_raised(
    importer, epg_bridge, factory, receiver
):
    """✅ The importer can say „running" for a day after a start that failed part-way."""
    bridge = epg_bridge()
    _start_that_raises_part_way(importer)
    send(factory)
    assert state(factory)["state"] == "failed"
    assert importer.epgimport.isImportRunning() is True

    factory.client.fire_message(ROOT + "/cmd/restart_gui", b"PRESS")
    assert error(factory) is None
    assert receiver.session.opened

    # The idle poll still reports what the importer says, and a second import
    # is still refused - only the power guard has lapsed.
    tick(bridge)
    assert state(factory)["state"] == "running"
    send(factory)
    assert error(factory) == epgimport.ALREADY_RUNNING
    assert bridge.run_command("reboot", "", PAGE) != "an EPG import is running"


def test_the_power_block_returns_once_the_stuck_import_has_ended(
    importer, epg_bridge, factory
):
    bridge = epg_bridge()
    _start_that_raises_part_way(importer)
    send(factory)
    tick(bridge)
    importer.epgimport.source = None
    tick(bridge)
    # A later import, started by the image, blocks the power commands again.
    importer.epgimport.source = object()
    tick(bridge)
    assert bridge.run_command("restart_gui", "", PAGE) == "an EPG import is running"


def test_the_power_block_lapses_once_the_watchdog_fired(importer, epg_bridge, factory, receiver):
    bridge = epg_bridge()
    publisher = bridge.publisher("epg_import")
    send(factory)
    assert bridge.run_command("restart_gui", "", PAGE) == "an EPG import is running"
    publisher._deadline = time.monotonic() - 1
    tick(bridge)
    assert state(factory)["state"] == "failed"

    factory.client.fire_message(ROOT + "/cmd/restart_gui", b"PRESS")
    assert error(factory) is None
    assert receiver.session.opened
    send(factory)
    assert error(factory) == epgimport.ALREADY_RUNNING


# ------------------------------------------------------ pinned by review (#27) --


def test_an_unreadable_schedule_setting_is_not_read_as_off(
    importer, epg_bridge, factory, monkeypatch
):
    """`getWakeTime()` answers -1 by itself when the schedule is off; ask it."""
    epg_bridge()
    monkeypatch.delattr(config.plugins.epgimport, "enabled")
    importer.autoStartTimer.wake = int(time.time()) + 300
    send(factory)
    assert error(factory).startswith("EPG-Importer's own scheduled import starts in")
    assert importer.started == 0


def test_already_running_is_refused_before_recording(importer, epg_bridge, factory, receiver):
    """The documented order: the running import is the reason, not the recording."""
    epg_bridge()
    importer.epgimport.source = object()
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    send(factory)
    assert error(factory) == epgimport.ALREADY_RUNNING


def test_a_restart_after_a_finished_import_does_not_announce_it_again(
    monkeypatch, epg_bridge, factory
):
    """`start()` takes a baseline: a settings save must not re-report the last import."""
    module = install_epg_importer(monkeypatch)
    module.lastImportResult = (time.time() - 600, 99)
    bridge = epg_bridge()
    grid = bridge.publisher("epg_grid")
    calls = []
    monkeypatch.setattr(grid, "regenerate", lambda: calls.append(1))
    tick(bridge)
    assert state(factory)["state"] == "idle"
    assert calls == []


def test_the_watchdog_fires_once(importer, epg_bridge, factory, plugin_log):
    bridge = epg_bridge()
    send(factory)
    bridge.publisher("epg_import")._deadline = time.monotonic() - 1
    tick(bridge)
    tick(bridge)
    tick(bridge)
    assert plugin_log().count("has not finished after 30 minutes") == 1


def test_an_unreadable_running_state_mid_run_is_not_the_end(importer, epg_bridge, factory):
    bridge = epg_bridge()
    send(factory)
    importer.epgimport.raises = True
    tick(bridge)
    assert state(factory)["state"] == "running"
    assert state(factory)["error"] is None
    importer.epgimport.raises = False
    importer.epgimport.finish(5)
    tick(bridge)
    assert state(factory)["state"] == "done"


def test_a_broken_importer_logs_its_traceback_once(importer, epg_bridge, plugin_log, settings):
    settings.log_level.value = "debug"
    bridge = epg_bridge()
    importer.epgimport.raises = True
    for _ in range(4):
        tick(bridge)
    log = plugin_log()
    assert log.count("Traceback") == 1
    assert log.count("isImportRunning() raised again") == 3


def test_a_start_that_raised_before_running_does_not_lapse_the_next_import(
    importer, epg_bridge, factory
):
    """The lapse ends at the first poll that finds nothing running."""
    bridge = epg_bridge()

    def broken():
        raise OSError("no network")

    real_start = importer.startImport
    importer.startImport = broken
    send(factory)
    assert state(factory)["state"] == "failed"
    tick(bridge)  # nothing is running

    importer.epgimport.sources = [FakeEpgSource("Polska - Podstawowy")]
    real_start()  # the image's own schedule, later
    tick(bridge)
    assert state(factory)["state"] == "running"
    assert bridge.run_command("restart_gui", "", PAGE) == "an EPG import is running"


# ------------------------------------------------------ review round 2 (#27) --


def test_an_early_start_failure_does_not_lapse_a_foreign_import_that_follows(
    importer, epg_bridge, factory, settings
):
    """🔴 No idle poll in between: the lapse must not cover the image's own import."""
    bridge = epg_bridge()
    settings.deep_standby_allowed.value = True

    def broken(*_arguments, **_keywords):
        raise OSError("settings unreadable")

    real_settings = importer.EPGConfig.loadUserSettings
    importer.EPGConfig.loadUserSettings = broken
    send(factory)
    assert state(factory)["state"] == "failed"
    importer.EPGConfig.loadUserSettings = real_settings

    # The importer's own „Manual" button, before the plugin polls again.
    importer.epgimport.sources = [FakeEpgSource("Polska - Podstawowy")]
    importer.startImport()
    assert bridge.run_command("restart_gui", "", PAGE) == "an EPG import is running"
    factory.client.fire_message(ROOT + "/cmd/reboot", b"PRESS")
    assert error(factory) == "an EPG import is running"


def test_a_successful_start_re_arms_the_power_block(importer, epg_bridge, factory):
    bridge = epg_bridge()
    real_start = importer.startImport
    _start_that_raises_part_way(importer)
    send(factory)
    assert bridge.run_command("restart_gui", "", PAGE) != "an EPG import is running"

    # The stuck run clears without a poll seeing it, and the next press works.
    importer.epgimport.source = None
    importer.startImport = real_start
    send(factory)
    assert state(factory)["state"] == "running"
    assert bridge.run_command("restart_gui", "", PAGE) == "an EPG import is running"


def test_a_press_refused_as_already_running_follows_that_import_at_once(
    importer, epg_bridge, factory
):
    """The refusal is never unexplained: the topic says `running` at the same moment."""
    bridge = epg_bridge()
    importer.epgimport.sources = [FakeEpgSource("Polska - Podstawowy")]
    importer.startImport()  # the image's schedule, between two idle polls
    assert state(factory)["state"] == "idle"
    before = int(time.time())
    send(factory)
    assert error(factory) == epgimport.ALREADY_RUNNING
    bridge.publisher("epg_import")._announce.timer.fire()  # the next main-loop turn
    payload = state(factory)
    assert payload["state"] == "running"
    assert before <= payload["started"] <= int(time.time())
    assert bridge.publisher("epg_import")._poll.timer.started == (2000, False)


def _foreign_import_between_polls(importer):
    importer.epgimport.sources = [FakeEpgSource("Polska - Podstawowy")]
    importer.startImport()


def test_a_refused_press_puts_the_refusal_out_before_the_running_topic(
    importer, epg_bridge, factory
):
    """🔴 A consumer reads the first new `running` as its press having worked."""
    bridge = epg_bridge()
    _foreign_import_between_polls(importer)
    mark = len(factory.client.published)
    send(factory)
    publisher = bridge.publisher("epg_import")
    assert publisher._announce.timer.started == (0, True)
    publisher._announce.timer.fire()

    order = []
    for entry in factory.client.published[mark:]:
        if entry.topic == LAST_ERROR and entry.text and "already running" in entry.text:
            order.append("last_error")
        elif entry.topic == TOPIC and entry.json()["state"] == "running":
            order.append("epg_import")
    assert order == ["last_error", "epg_import"]


def test_the_follow_reaches_the_topic_by_the_poll_when_no_turn_is_given(
    importer, epg_bridge, factory
):
    """No announcement timer: the two-second poll publishes it, still after the refusal."""
    bridge = epg_bridge()
    _foreign_import_between_polls(importer)
    publisher = bridge.publisher("epg_import")
    publisher._announce.start = lambda *_arguments, **_keywords: False
    send(factory)
    assert state(factory)["state"] == "idle"
    tick(bridge)
    assert state(factory)["state"] == "running"


def test_a_second_refused_press_does_not_restart_the_follow(importer, epg_bridge, factory):
    """`started` and the watchdog belong to the import, not to the latest press."""
    bridge = epg_bridge()
    _foreign_import_between_polls(importer)
    send(factory)
    publisher = bridge.publisher("epg_import")
    publisher._announce.timer.fire()
    publisher._started -= 120
    started, deadline = publisher._started, publisher._deadline

    send(factory)
    assert error(factory) == epgimport.ALREADY_RUNNING
    assert publisher._started == started
    assert publisher._deadline == deadline


def test_a_stuck_start_seen_idle_by_a_poll_re_arms_the_block(importer, epg_bridge, factory):
    """The lapse ends at the first poll that finds the importer idle."""
    bridge = epg_bridge()
    real_start = importer.startImport
    _start_that_raises_part_way(importer)
    send(factory)
    importer.epgimport.source = None  # the stuck run clears between two polls
    tick(bridge)

    importer.epgimport.sources = [FakeEpgSource("Polska - Podstawowy")]
    real_start()  # the image's own schedule, later
    tick(bridge)
    assert state(factory)["state"] == "running"
    assert bridge.run_command("restart_gui", "", PAGE) == "an EPG import is running"
