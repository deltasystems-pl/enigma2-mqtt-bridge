"""Every template the discovery payloads carry, rendered against what the box publishes.

A template matched as a string is a template nobody has run. That is how
„next timer" shipped with the offset written twice and read unknown for two
releases: the assertion checked that the string said what the author meant,
and the string did. So nothing here compares template text. Each template is
rendered with the engine in `hatemplate.py` — Home Assistant's filters where
they differ from Jinja's, and nothing it does not own — against the bytes the
plugin's own publishers produce, and the assertion is about what the entity
would receive.

Two rules keep this honest:

* **The payloads come from the code that publishes them**, encoded by the
  bridge's own encoder — never from a dictionary typed here. A hand-written
  payload tests the template against the author's idea of the payload, which is
  the same mistake one level down. Where a case needs a key to be *absent*, the
  real payload is taken and the key removed from it, because an older plugin or
  a truncated message is exactly that.
* **Nothing can be left out by accident.** `test_every_template_the_plugin_emits_is_rendered`
  builds the discovery payload with every capability and every permission a box
  can have, and fails if a template in it has no rendering test. A new templated
  component without one is a red build, not a gap nobody sees.
"""

import datetime
import inspect
import json

import hatemplate
import pytest
import test_process
import test_softcam
from conftest import POLSAT, TVP1, RecordTimerEntry, install_epg_importer

from MQTTBridge import bridge as bridge_module
from MQTTBridge import discovery, hdd, keys, process, recording, volume
from MQTTBridge.publishers import PUBLISHER_CLASSES

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
NAME = "Living room receiver"

# The softcam's receiver and bridge, as `test_softcam` builds them: a `/proc`
# on disk with a supervisor and its worker, and a bridge pointed at it.
lab = test_softcam.lab
softcam_bridge = test_softcam.softcam_bridge
# The enigma2 process's `/proc`, as `test_process` builds it.
proc = test_process.proc


# ----------------------------------------------------------- the templates --

# Filled at import time by `@renders`: which templates have a rendering test.
COVERED = set()


def renders(*names):
    """Mark a test as the one that renders these templates."""

    def mark(function):
        COVERED.update(names)
        return function

    return mark


def every_capability():
    """Every capability a bridge can ever claim: the core ones, and one per publisher."""
    names = set(bridge_module.CORE_CAPABILITIES)
    names.update(publisher.name for publisher in PUBLISHER_CLASSES if publisher.name)
    names.add(bridge_module.MESSAGE_CAPABILITY)
    return sorted(names)


def everything():
    """The discovery topics of a box that can do everything and has been allowed all of it.

    Every `*_allowed` keyword is found by name rather than listed, so a
    permission added later gates its component in here without anybody
    remembering to.
    """
    parameters = inspect.signature(discovery.build_discovery_components).parameters
    allowed = {name: True for name in parameters if name.endswith("_allowed")}
    info = {"capabilities": every_capability(), "boxtype": "vuuno4kse", "plugin": "0.0.0"}
    return discovery.build_discovery_components(
        NODE, NAME, "enigma2", info,
        channel_options=["TVP 1 HD", "TVN HD", "Polsat Sport"],
        **allowed,
    )


def templates():
    """{"component.field" or "trigger/<name>.field": template} for every template emitted."""
    found = {}
    for topic, payload in everything().items():
        if "/device_automation/" in topic:
            owner = "trigger/" + topic.split("/")[-2]
            fields = payload
        else:
            fields = {}
            for key, component in payload["cmps"].items():
                for field, value in component.items():
                    fields[key + "." + field] = value
            owner = None
        for field, value in fields.items():
            if field.split(".")[-1].endswith("_tpl"):
                found[field if owner is None else owner + "." + field] = value
    return found


TEMPLATES = templates()


def template(name):
    return TEMPLATES[name]


def render(name, payload):
    return hatemplate.render_payload(template(name), payload)


def attributes(name, payload):
    """A `json_attr_tpl`: Home Assistant keeps the result only if it is a JSON object."""
    found = json.loads(render(name, payload))
    assert isinstance(found, dict)
    return found


# ------------------------------------------------------------ the payloads --


def wire(bridge, publisher, suffix=None):
    """What a publisher would put on the wire now, as the bridge encodes it."""
    snapshot = bridge.publisher(publisher).snapshot()
    return bridge_module._encoded(snapshot[suffix or publisher])


def encoded(payload):
    return bridge_module._encoded(payload)


def without(text, key):
    """The same payload with one key missing: an older plugin, or a truncated message."""
    payload = json.loads(text)
    del payload[key]
    return encoded(payload)


def iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)


# ------------------------------------------------------ coverage and guard --


def test_every_template_the_plugin_emits_is_rendered():
    assert set(TEMPLATES) == COVERED


def test_the_full_payload_really_has_every_optional_template():
    """The coverage test is only as good as `everything()`: prove it reaches the gated ones."""
    for name in ("softcam.val_tpl", "epg_import.json_attr_tpl", "channel_select.cmd_tpl",
                 "process_started.val_tpl", "trigger/blue_long.val_tpl"):
        assert name in TEMPLATES


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_no_template_uses_anything_the_engine_does_not_own(name):
    hatemplate.check(TEMPLATES[name])


@pytest.mark.parametrize(
    "source, missing",
    [
        ("{{ value_json.x | float }}", "filter float"),
        ("{{ value_json.x | lower }}", "filter lower"),
        ("{% if value_json.x is defined %}1{% endif %}", "test defined"),
        ("{{ now() }}", "global now"),
        ("{% filter upper %}{{ value }}{% endfilter %}", "filter upper"),
        # On a branch no payload in this module would ever take.
        ("{% if false %}{{ value_json.x | as_timestamp }}{% endif %}", "filter as_timestamp"),
    ],
)
def test_the_guard_refuses_what_the_engine_does_not_own(source, missing):
    """A filter nobody copied must fail the build, not render with Jinja's semantics."""
    with pytest.raises(AssertionError, match=missing):
        hatemplate.render(source, value="{}", value_json={})


def test_the_environment_holds_only_what_is_listed():
    """Empty, then filled: nothing of Jinja's is inherited by name."""
    assert set(hatemplate.ENVIRONMENT.filters) == set(hatemplate.FILTERS)
    assert set(hatemplate.ENVIRONMENT.tests) == set(hatemplate.TESTS)
    assert set(hatemplate.ENVIRONMENT.globals) == set(hatemplate.GLOBALS)


def test_the_result_is_stripped_as_home_assistant_strips_it():
    assert hatemplate.render("  {{ 'ON' }}\n") == "ON"


def test_a_template_variable_and_a_set_name_are_not_globals():
    filters, tests, globals_ = hatemplate.used_names(
        "{% set kb = value_json.a | default(none) %}{{ kb if kb is not none else value }}"
    )
    assert (filters, tests, globals_) == ({"default"}, {"none"}, set())


# ------------------------------------------------ the engine is Home Assistant's --


def test_int_raises_where_jinja_would_answer_zero():
    """Home Assistant's `int`, not Jinja's: a null epoch is an error, not 1970."""
    with pytest.raises(ValueError, match="int got invalid input 'None'"):
        hatemplate.render("{{ value_json.x | int }}", value_json={"x": None})
    assert hatemplate.render("{{ value_json.x | int(7) }}", value_json={"x": None}) == "7"
    assert hatemplate.render("{{ value_json.x | int }}", value_json={"x": "42.9"}) == "42"


def test_round_is_an_int_at_precision_zero_and_refuses_none():
    assert hatemplate.render("{{ 2.5 | round }}") == "2"
    assert hatemplate.render("{{ 1.25 | round(1) }}") == "1.2"
    with pytest.raises(ValueError, match="round got invalid input 'None'"):
        hatemplate.render("{{ none | round(1) }}")


def test_timestamp_utc_already_carries_the_offset():
    assert hatemplate.render("{{ 0 | timestamp_utc }}") == "1970-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="timestamp_utc got invalid input"):
        hatemplate.render("{{ 'soon' | timestamp_utc }}")


def test_default_fires_on_an_absent_key_and_not_on_a_null():
    assert hatemplate.render_payload("{{ value_json.x | default(none) }}", "{}") == "None"
    assert hatemplate.render_payload("{{ value_json.x }}", "{}") == ""
    assert hatemplate.render_payload("{{ value_json.x | default(5) }}", '{"x": null}') == "None"


# ---------------------------------------------------------------- service --


@renders("channel.val_tpl", "channel.json_attr_tpl", "channel_select.val_tpl")
def test_the_channel(live_bridge, receiver):
    payload = wire(live_bridge, "service")

    assert render("channel.val_tpl", payload) == "TVP 1 HD"
    assert render("channel_select.val_tpl", payload) == "TVP 1 HD"
    assert attributes("channel.json_attr_tpl", payload) == {
        "sref": TVP1, "bouquet": "Ulubione TV", "provider": "Cyfrowy Polsat",
        "width": 1920, "height": 1080,
    }

    # 🔴 The `| default(none)` case: the name absent, not null.
    for name in ("channel.val_tpl", "channel_select.val_tpl"):
        assert render(name, without(payload, "name")) == "None"

    receiver.nav.sref = ""
    receiver.nav.service = None
    nothing = wire(live_bridge, "service")
    assert render("channel.val_tpl", nothing) == "None"
    assert render("channel_select.val_tpl", nothing) == "None"
    assert set(attributes("channel.json_attr_tpl", nothing).values()) == {None}


@renders("channel_select.cmd_tpl")
def test_the_channel_list_command_is_something_the_box_accepts(live_bridge, factory, receiver):
    """Rendered, then handed back to the plugin's own `cmd/zap`, which must act on it."""
    options = everything()["homeassistant/device/" + NODE + "/config"]["cmps"][
        "channel_select"]["ops"]
    for option in options + ['4" News \\ HD', "Łódź TV"]:
        rendered = hatemplate.render(template("channel_select.cmd_tpl"), value=option)
        assert json.loads(rendered) == {"name": option}

    rendered = hatemplate.render(template("channel_select.cmd_tpl"), value="Polsat Sport")
    factory.client.fire_message(ROOT + "/cmd/zap", rendered.encode("utf-8"))
    assert receiver.nav.played == [POLSAT]


# -------------------------------------------------------------------- epg --


@renders("program.val_tpl", "program.json_attr_tpl", "next_program.val_tpl")
def test_the_programme(live_bridge, receiver):
    payload = wire(live_bridge, "epg")

    assert render("program.val_tpl", payload) == "Wiadomości"
    assert render("next_program.val_tpl", payload) == "Pogoda"
    assert attributes("program.json_attr_tpl", payload) == {
        "begin": 1789459200, "end": 1789460700, "event_id": 27431,
        "short": "Serwis informacyjny", "long": "",
        "next_title": "Pogoda", "next_begin": 1789460700, "next_end": 1789461000,
    }

    # Only one programme known: `next` is null, which must not be an error.
    receiver.info.events = receiver.info.events[:1]
    one = wire(live_bridge, "epg")
    assert json.loads(one)["next"] is None
    assert render("program.val_tpl", one) == "Wiadomości"
    assert render("next_program.val_tpl", one) == "None"
    found = attributes("program.json_attr_tpl", one)
    assert found["event_id"] == 27431
    assert (found["next_title"], found["next_begin"], found["next_end"]) == (None, None, None)

    # Nothing playing at all.
    receiver.nav.service = None
    none = wire(live_bridge, "epg")
    assert render("program.val_tpl", none) == "None"
    assert render("next_program.val_tpl", none) == "None"
    assert set(attributes("program.json_attr_tpl", none).values()) == {None}


# -------------------------------------------------------------- recording --


@renders("recording.val_tpl", "active_recordings.val_tpl", "next_timer.val_tpl")
def test_recording_and_the_next_timer(live_bridge, receiver, monkeypatch):
    idle = wire(live_bridge, "recording")
    assert render("recording.val_tpl", idle) == "OFF"
    assert render("active_recordings.val_tpl", idle) == "0"
    assert render("next_timer.val_tpl", idle) == "None"

    monkeypatch.setattr(recording.time, "time", lambda: 1789000000)
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    receiver.add_timer(begin=1789500000, end=1789501000, name="Later")
    busy = wire(live_bridge, "recording")
    assert render("recording.val_tpl", busy) == "ON"
    assert render("active_recordings.val_tpl", busy) == "1"
    # A timestamp sensor takes exactly one shape: ISO 8601 with a zone.
    rendered = render("next_timer.val_tpl", busy)
    assert datetime.datetime.fromisoformat(rendered) == iso(1789500000)
    assert rendered.endswith("+00:00") and rendered.count("+00:00") == 1


# ----------------------------------------------------------------- volume --


@renders("volume.val_tpl", "mute.val_tpl")
def test_volume_and_mute(live_bridge):
    payload = wire(live_bridge, "volume")
    assert render("volume.val_tpl", payload) == "35"
    assert render("mute.val_tpl", payload) == "OFF"

    assert volume.set_muted(True) is None
    assert render("mute.val_tpl", wire(live_bridge, "volume")) == "ON"


# ------------------------------------------------------------------ tuner --


@renders("snr.val_tpl", "agc.val_tpl", "ber.val_tpl")
def test_the_signal(live_bridge, receiver):
    payload = wire(live_bridge, "tuner")
    assert [render(k + ".val_tpl", payload) for k in ("snr", "agc", "ber")] == ["80", "61", "0"]

    for key in ("snr", "agc", "ber"):
        assert render(key + ".val_tpl", without(payload, key)) == "None"

    receiver.service._frontend = None
    nothing = wire(live_bridge, "tuner")
    assert [render(k + ".val_tpl", nothing) for k in ("snr", "agc", "ber")] == ["None"] * 3


# ---------------------------------------------------------------- softcam --


@renders("softcam.val_tpl", "softcam.json_attr_tpl")
def test_the_softcam(softcam_bridge):
    bridge = softcam_bridge()
    payload = wire(bridge, "softcam")

    assert render("softcam.val_tpl", payload) == test_softcam.CAM
    assert attributes("softcam.json_attr_tpl", payload) == {
        "running_instances": 1,
        "last_restart": None,
        "last_restart_reason": None,
        "restarts_today": 0,
        "manager_check_on_start": True,
        "manager_timer_minutes": None,
    }
    assert render("softcam.val_tpl", without(payload, "selected")) == "None"


# ------------------------------------------------------------- epg import --


@renders("epg_import.val_tpl", "epg_import.json_attr_tpl")
def test_the_epg_import(make_bridge, factory, settings, receiver, monkeypatch):
    importer = install_epg_importer(monkeypatch)
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.friendly_name.value = NAME
    settings.epg_import_allowed.value = True
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()

    idle = wire(bridge, "epg_import")
    assert render("epg_import.val_tpl", idle) == json.loads(idle)["state"]

    factory.client.fire_message(ROOT + "/cmd/epg_import", b"PRESS")
    importer.epgimport.finish(12)
    bridge.publisher("epg_import")._poll.timer.fire()
    done = wire(bridge, "epg_import")
    published = json.loads(done)

    assert render("epg_import.val_tpl", done) == "done"
    assert attributes("epg_import.json_attr_tpl", done) == {
        "started": published["started"], "finished": published["finished"],
        "events": 12, "error": None,
    }
    assert render("epg_import.val_tpl", without(done, "state")) == "None"


# -------------------------------------------------------------------- hdd --


@renders("recording_disk.val_tpl")
def test_the_recording_disk(live_bridge):
    assert render("recording_disk.val_tpl", wire(live_bridge, "hdd")) == "OFF"
    # `/` is a mount point on every machine this runs on; `read` is what the
    # publisher publishes.
    mounted = encoded(hdd.read("/"))
    assert json.loads(mounted)["mounted"] is True
    assert render("recording_disk.val_tpl", mounted) == "ON"


# ---------------------------------------------------------------- process --


@renders(
    "process_memory.val_tpl", "process_memory_peak.val_tpl", "process_threads.val_tpl",
    "process_open_files.val_tpl", "process_started.val_tpl",
)
def test_the_process(proc, tmp_path):
    payload = encoded(process.ProcessPublisher(proc_root=str(proc)).snapshot()["process"])

    # 164208 kB and 187432 kB, in MiB to one place — a float, as Home Assistant's
    # `round(1)` returns it.
    assert render("process_memory.val_tpl", payload) == "160.4"
    assert render("process_memory_peak.val_tpl", payload) == "183.0"
    assert render("process_threads.val_tpl", payload) == "22"
    assert render("process_open_files.val_tpl", payload) == "7"
    started = render("process_started.val_tpl", payload)
    assert datetime.datetime.fromisoformat(started) == iso(test_process.EXPECTED_STARTED)
    assert started.count("+00:00") == 1

    # Unreadable: every field null. Unknown, not an error and not zero.
    unreadable = process.ProcessPublisher(proc_root=str(tmp_path / "nothing"))
    nulls = encoded(unreadable.snapshot()["process"])
    assert set(json.loads(nulls).values()) == {None}
    for name in ("process_memory", "process_memory_peak", "process_threads",
                 "process_open_files", "process_started"):
        assert render(name + ".val_tpl", nulls) == "None", name

    # 🔴 Absent — the case `| default(none)` exists for. Without it an absent
    # key renders empty, which Home Assistant reads as „ignore this message".
    for name, key in (("process_memory", "rss_kb"), ("process_memory_peak", "hwm_kb"),
                      ("process_threads", "threads"), ("process_open_files", "fds"),
                      ("process_started", "started")):
        assert render(name + ".val_tpl", without(payload, key)) == "None", name


# ------------------------------------------------------------------- info --


@renders("uptime.val_tpl")
def test_the_uptime(live_bridge):
    payload = encoded(live_bridge.build_info())
    uptime = json.loads(payload)["uptime"]
    assert isinstance(uptime, int)
    assert render("uptime.val_tpl", payload) == str(uptime)
    assert render("uptime.val_tpl", without(payload, "uptime")) == "None"


# ------------------------------------------------------------------- keys --


COLOUR_CODES = {name: code for code, name in keys.KEY_NAMES.items() if name in keys.COLOUR_KEYS}
MAKE, BREAK, REPEAT, LONG = 0, 1, 2, 3


@renders(*sorted(name for name in TEMPLATES if name.startswith("trigger/")))
def test_each_trigger_matches_its_own_press_and_no_other(live_bridge, factory):
    """Rendered against the press the key publisher really sends, compared as HA compares it."""
    triggers = {
        topic.split("/")[-2]: payload
        for topic, payload in everything().items()
        if "/device_automation/" in topic
    }
    handler = live_bridge.publisher("keys")._on_key
    for colour in keys.COLOUR_KEYS:
        for flags, press in (((MAKE, BREAK), keys.PRESS_SHORT), ((MAKE, LONG), keys.PRESS_LONG)):
            for flag in flags:
                handler(COLOUR_CODES[colour], flag)
            sent = factory.client.last(ROOT + "/key").text
            if press == keys.PRESS_LONG:
                handler(COLOUR_CODES[colour], BREAK)
            name = colour.replace("KEY_", "").lower() + "_" + press
            fired = [
                other for other, trigger in triggers.items()
                if hatemplate.render_payload(trigger["val_tpl"], sent) == trigger["pl"]
            ]
            assert fired == [name]
