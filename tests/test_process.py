"""`process`: what the enigma2 process costs, published on a steady cadence.

Everything is read against a fixture `/proc` rather than against the machine
running the tests, because the question these tests answer is „does this parse
what a receiver's kernel writes" — including the parts of that format nobody
thinks about until it bites: a `comm` field with a closing parenthesis in it, a
`SC_CLK_TCK` that is not 100, and a file that has stopped being readable
halfway through the life of the process.
"""

import datetime
import os

import pytest
from conftest import eTimer, render_value_template

from MQTTBridge import discovery, process

NODE = "vuuno4kse_005301"


def kibibytes(field):
    """The guarded kB-to-MiB rendering, written out here rather than imported.

    Spelling it a second time is the point: a change to the template has to be
    made twice, on purpose, rather than being asserted against itself.
    """
    return (
        "{% set kb = value_json." + field + " | default(none) %}"
        "{{ (kb / 1024) | round(1) if kb is not none else none }}"
    )

# A real OpenViX 6.6 status file, cut to the lines this reads plus enough of its
# neighbours that a parser matching the wrong prefix would be caught.
STATUS = """Name:\tenigma2
State:\tS (sleeping)
Tgid:\t1204
Pid:\t1204
VmPeak:\t  412300 kB
VmSize:\t  398120 kB
VmLck:\t       0 kB
VmHWM:\t  187432 kB
VmRSS:\t  164208 kB
RssAnon:\t  140112 kB
VmData:\t  221004 kB
VmStk:\t     132 kB
Threads:\t22
SigQ:\t0/3597
"""

# Field 2 is `(enigma2)` on a receiver; here it carries a space **and** a
# closing parenthesis, both of which are legal and neither of which is escaped.
# The space alone breaks a naive `split()[21]`; the inner `)` is what tells
# `rpartition(")")` apart from `partition(")")`, which agree on every `comm`
# that does not contain one.
STAT = (
    "1204 (enig) ma2) S 1 1204 1204 0 -1 4194560 88213 0 141 0 "
    "3271 918 0 0 20 0 22 0 "
    "4210987 "
    "407674880 164208 18446744073709551615 1 1 0 0 0 0 0 4096 16899 0 0 0 17 1 0 0 0 0 0\n"
)

BTIME = 1789000000
CLOCK_TICKS = 100
# STAT's field 22 is 4210987 ticks, which at 100 Hz is 42109 whole seconds.
EXPECTED_STARTED = BTIME + 4210987 // CLOCK_TICKS


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def proc(tmp_path, monkeypatch):
    """A `/proc` with the four things this module reads."""
    root = tmp_path / "proc"
    write(root / "self" / "status", STATUS)
    write(root / "self" / "stat", STAT)
    write(root / "stat", f"cpu  1 2 3\nbtime {BTIME}\nprocesses 4321\n")
    descriptors = root / "self" / "fd"
    descriptors.mkdir(parents=True)
    for number in range(7):
        (descriptors / str(number)).write_text("", encoding="utf-8")
    monkeypatch.setattr(process, "clock_ticks", lambda: CLOCK_TICKS)
    return root


# -------------------------------------------------------------- the payload --


def test_the_payload_is_the_documented_five_fields(proc):
    assert process.read(str(proc)) == {
        "rss_kb": 164208,
        "hwm_kb": 187432,
        "threads": 22,
        "fds": 7,
        "started": EXPECTED_STARTED,
    }


def test_the_keys_are_always_there_even_when_nothing_can_be_read(tmp_path):
    """An absent key renders as an empty string in a template, which means „keep
    the old value". An explicit null renders as unknown, which is the truth."""
    found = process.read(str(tmp_path / "no-proc-here"))

    assert sorted(found) == sorted(process.FIELDS)
    assert set(found.values()) == {None}


def test_one_unreadable_field_does_not_take_the_others_with_it(proc):
    os.remove(str(proc / "self" / "stat"))

    found = process.read(str(proc))

    assert found["started"] is None
    assert found["rss_kb"] == 164208
    assert found["threads"] == 22


def test_a_status_line_that_is_not_a_number_is_null(proc):
    write(proc / "self" / "status", STATUS.replace("164208 kB", "unknown kB"))

    assert process.read(str(proc))["rss_kb"] is None
    assert process.read(str(proc))["hwm_kb"] == 187432


def test_a_comm_field_with_a_parenthesis_in_it_is_parsed(proc):
    """`/proc/<pid>/stat` field 2 is unescaped; the tail starts at the LAST `)`."""
    assert process.started_at(str(proc)) == EXPECTED_STARTED


def test_started_needs_the_boot_time(proc):
    write(proc / "stat", "cpu  1 2 3\nprocesses 4321\n")

    assert process.started_at(str(proc)) is None


def test_started_needs_a_clock_rate(proc, monkeypatch):
    monkeypatch.setattr(process, "clock_ticks", lambda: None)

    assert process.started_at(str(proc)) is None


def test_a_clock_rate_that_is_not_a_hundred_is_used(proc, monkeypatch):
    monkeypatch.setattr(process, "clock_ticks", lambda: 250)

    assert process.started_at(str(proc)) == BTIME + 4210987 // 250


def test_the_clock_rate_is_asked_of_the_platform_not_assumed():
    # 100 everywhere this will ever run, and never hard-coded anyway.
    assert process.clock_ticks() in (None, os.sysconf("SC_CLK_TCK"))


def test_a_truncated_stat_line_is_null(proc):
    write(proc / "self" / "stat", "1204 (enigma2) S 1 1204\n")

    assert process.started_at(str(proc)) is None


def test_open_files_counts_the_descriptor_directory(proc):
    assert process.open_files(str(proc)) == 7
    assert process.open_files(str(proc / "absent")) is None


# ------------------------------------------------------------- the publisher --


class Bridge:
    def __init__(self):
        self.published = []

    def publish_state(self, suffix, payload, raw=False, retain=True, volatile=()):
        self.published.append((suffix, payload, raw, retain))


def publisher(proc):
    bridge = Bridge()
    return process.ProcessPublisher(bridge, str(proc)), bridge


def test_the_capability_is_claimed_when_proc_answers(proc):
    found, _bridge = publisher(proc)

    assert found.start() is True
    assert found.claimed() is True
    assert found.name == "process"


def test_the_capability_is_refused_when_proc_does_not_answer(tmp_path):
    found, _bridge = publisher(tmp_path / "no-proc-here")

    assert found.start() is False
    assert found.claimed() is False
    # Not „switched off": there is no setting for this, so the log has to blame
    # the image rather than somebody's choice.
    assert found.switched_off is False


def test_the_capability_stops_being_claimed_when_proc_stops_answering(proc):
    found, _bridge = publisher(proc)
    found.start()
    os.remove(str(proc / "self" / "status"))

    found._tick()

    assert found.claimed() is False


def test_the_snapshot_is_read_fresh(proc):
    found, _bridge = publisher(proc)
    found.start()
    write(proc / "self" / "status", STATUS.replace("164208 kB", "170000 kB"))

    assert found.snapshot()["process"]["rss_kb"] == 170000


def test_the_snapshot_starts_the_cadence(proc):
    """The bridge publishes the snapshot itself, so „every 300 s" runs from it."""
    found, bridge = publisher(proc)
    found.start()
    found.snapshot()

    found._tick()

    assert bridge.published == []


def test_a_tick_before_any_publish_publishes(proc):
    found, bridge = publisher(proc)
    found.start()

    found._tick()

    assert bridge.published == [("process", found._cached, False, True)]
    assert bridge.published[0][1]["rss_kb"] == 164208


def test_a_small_move_does_not_publish_early(proc):
    found, bridge = publisher(proc)
    found.start()
    found.snapshot()
    write(proc / "self" / "status", STATUS.replace("164208 kB", "168303 kB"))

    found._tick()

    assert bridge.published == []


def test_a_move_of_four_megabytes_publishes_early(proc):
    found, bridge = publisher(proc)
    found.start()
    found.snapshot()
    write(proc / "self" / "status", STATUS.replace("164208 kB", "168304 kB"))

    found._tick()

    assert [entry[1]["rss_kb"] for entry in bridge.published] == [168304]


def test_a_move_downwards_publishes_early_too(proc):
    """A resident set that fell by 4 MiB is as interesting as one that rose."""
    found, bridge = publisher(proc)
    found.start()
    found.snapshot()
    write(proc / "self" / "status", STATUS.replace("164208 kB", "160112 kB"))

    found._tick()

    assert [entry[1]["rss_kb"] for entry in bridge.published] == [160112]


def test_the_full_cadence_publishes_a_still_receiver(proc, monkeypatch):
    """Nothing about the box changed; the curve still gets a point every 300 s."""
    clock = [1000.0]
    # Patched before the snapshot, because the snapshot is what starts the clock.
    monkeypatch.setattr(process.time, "monotonic", lambda: clock[0])
    found, bridge = publisher(proc)
    found.start()
    found.snapshot()

    clock[0] = 1000.0 + process.FULL_INTERVAL_SECONDS - 1
    found._tick()
    assert bridge.published == []

    clock[0] = 1000.0 + process.FULL_INTERVAL_SECONDS
    found._tick()
    assert len(bridge.published) == 1


def test_an_unreadable_resident_set_does_not_publish_early(proc):
    found, bridge = publisher(proc)
    found.start()
    found.snapshot()
    write(proc / "self" / "status", STATUS.replace("VmRSS:", "VmNotRSS:"))

    found._tick()

    assert bridge.published == []


def test_the_cadence_constants_are_the_contract():
    assert process.FULL_INTERVAL_SECONDS == 300
    assert process.RSS_STEP_KB == 4096
    assert process.POLL_MILLISECONDS == 60000


def test_a_read_that_raises_does_not_reach_the_main_loop(proc, monkeypatch):
    found, bridge = publisher(proc)
    found.start()
    monkeypatch.setattr(process, "open_files", lambda _root: 1 / 0)

    # The Ticker swallows it, but nothing should have to: `_tick` returns.
    found._tick()

    assert bridge.published[-1][1]["fds"] is None


def test_stop_leaves_no_live_timer(proc):
    found, _bridge = publisher(proc)
    found.start()
    assert len(eTimer.instances) == 1
    timer = eTimer.instances[0]

    found.stop()

    assert timer.stopped is True
    assert timer.callback == []
    assert found._ticker.timer is None


def test_a_publisher_that_never_started_stops_without_a_timer(tmp_path):
    found, _bridge = publisher(tmp_path / "no-proc-here")
    found.start()
    found.stop()

    assert eTimer.instances == []


# ------------------------------------------------------- on a running bridge --
#
# These run against the machine's own `/proc`, which is the point: every other
# test here proves the parser against a fixture, and this one proves that a
# Linux kernel writes what the parser expects.

PROCESS_TOPIC = "enigma2/" + NODE + "/process"


def test_the_topic_is_in_the_snapshot_on_connect(live_bridge, factory):
    published = factory.client.last(PROCESS_TOPIC)

    assert published is not None
    assert published.retain is True
    assert published.qos == 0
    assert sorted(published.json()) == sorted(process.FIELDS)
    assert published.json()["rss_kb"] > 0


def test_the_capability_is_announced(live_bridge):
    assert "process" in live_bridge.capabilities()


def test_the_registry_puts_it_next_to_the_other_box_health(live_bridge):
    from MQTTBridge import publishers

    names = [cls.name for cls in publishers.PUBLISHER_CLASSES]
    assert names.index("process") == names.index("hdd") + 1


def test_reset_retracts_it_and_puts_it_straight_back(live_bridge, factory):
    assert live_bridge.state.knows(PROCESS_TOPIC)
    factory.client.clear()

    live_bridge.reset_retained()

    sent = [entry for entry in factory.client.all_for(PROCESS_TOPIC)]
    assert sent[0].text == ""
    assert sent[0].retain is True
    assert sent[-1].json()["rss_kb"] > 0


# --------------------------------------------------------- the HA components --


def device_components(factory):
    topic = "homeassistant/device/" + NODE + "/config"
    return factory.client.last(topic).json()["cmps"]


def test_five_diagnostic_sensors_are_announced(live_bridge, factory):
    found = device_components(factory)
    keys = [key for key in found if key.startswith("process")]

    assert sorted(keys) == [
        "process_memory",
        "process_memory_peak",
        "process_open_files",
        "process_started",
        "process_threads",
    ]
    for key in keys:
        assert found[key]["p"] == "sensor"
        assert found[key]["uniq_id"] == NODE + "_" + key
        assert found[key]["ent_cat"] == "diagnostic"
        assert found[key]["stat_t"] == PROCESS_TOPIC


def test_the_resident_set_is_the_one_enabled_by_default(live_bridge, factory):
    found = device_components(factory)

    memory = found["process_memory"]
    assert "en" not in memory
    assert memory["val_tpl"] == kibibytes("rss_kb")
    assert memory["unit_of_meas"] == "MiB"
    assert memory["dev_cla"] == "data_size"
    assert memory["stat_cla"] == "measurement"

    for key in ("process_memory_peak", "process_threads", "process_open_files",
                "process_started"):
        assert found[key]["en"] is False


def test_the_peak_reads_the_high_water_mark(live_bridge, factory):
    peak = device_components(factory)["process_memory_peak"]

    assert peak["val_tpl"] == kibibytes("hwm_kb")
    assert peak["unit_of_meas"] == "MiB"


def test_neither_memory_template_divides_something_that_might_be_null(live_bridge, factory):
    """`null / 1024` is a template error, and a template error is not „unknown".

    It leaves the previous reading on screen for ever with a line in a log
    nobody reads — which is exactly the failure a diagnostic sensor exists to
    make visible. `| default(none)` comes first so that an absent key takes the
    same branch as an explicit null rather than being Undefined and going down
    the arithmetic path after all.
    """
    found = device_components(factory)

    for key, field in (("process_memory", "rss_kb"), ("process_memory_peak", "hwm_kb")):
        template = found[key]["val_tpl"]
        assert "value_json." + field + " | default(none)" in template
        assert "if kb is not none else none" in template
        # The division never sees the raw field.
        assert "value_json." + field + " / 1024" not in template


def test_the_counts_are_measurements(live_bridge, factory):
    found = device_components(factory)

    assert found["process_threads"]["val_tpl"] == "{{ value_json.threads | default(none) }}"
    assert found["process_open_files"]["val_tpl"] == "{{ value_json.fds | default(none) }}"
    for key in ("process_threads", "process_open_files"):
        assert found[key]["stat_cla"] == "measurement"
        assert "unit_of_meas" not in found[key]


def test_the_start_time_is_rendered_as_a_zoned_timestamp(live_bridge, factory):
    """A timestamp sensor takes neither epoch seconds nor a time without a zone.

    Rendered, not matched as a string. The assertion this replaces checked that
    `+00:00` was in the template, which was true and was the defect: the filter
    before it already ends in the offset, so the state came out with two of them
    and Home Assistant stored nothing at all.
    """
    started = device_components(factory)["process_started"]

    assert started["dev_cla"] == "timestamp"
    assert "stat_cla" not in started

    rendered = render_value_template(started["val_tpl"], {"started": 1789042109})
    assert datetime.datetime.fromisoformat(rendered) == datetime.datetime(
        2026, 9, 10, 12, 8, 29, tzinfo=datetime.timezone.utc
    )

    # A reading that is not there is the literal None, which Home Assistant's
    # MQTT sensor turns into the unknown state before it parses anything.
    assert render_value_template(started["val_tpl"], {"started": None}) == "None"


def test_a_box_without_the_capability_gets_no_process_entities():
    found = discovery.build_discovery_components(
        NODE, "Living room receiver", "enigma2", {"capabilities": ["power"]}
    )
    components = found[discovery.device_topic("homeassistant", NODE)]["cmps"]

    assert [key for key in components if key.startswith("process")] == []


def test_a_component_that_stops_being_announced_is_removed_by_name():
    """Left out of a republished payload it would be kept, not removed."""
    found = discovery.build_discovery_components(
        NODE,
        "Living room receiver",
        "enigma2",
        {"capabilities": ["power"]},
        previous={"process_memory": "sensor"},
    )
    components = found[discovery.device_topic("homeassistant", NODE)]["cmps"]

    assert components["process_memory"] == {"p": "sensor"}
