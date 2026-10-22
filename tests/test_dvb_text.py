"""DVB's control codes, U+0080 to U+009F, never leave the receiver in a name or a description.

enigma2 hands a service's name and an event's through with the codes the broadcaster put in -
emphasis on (0x86) and off (0x87), a line break (0x8A) - and a reader of the published name sees
boxes or nothing: `\\x86Sky \\x87Cinema`. They are taken out where the name is read from the
image (`enigma2.dvb_text`), so every topic carries one spelling of it.
"""

import json

import pytest
from conftest import TVN, TVP1, RecordTimerEntry, ServiceReference
from pvrimage import PvrImage, inline, meta_text
from test_recording import RECORDING, TIMERS, fire_coalesced

from MQTTBridge import enigma2 as enigma2_module
from MQTTBridge import library
from MQTTBridge.enigma2 import dvb_text
from MQTTBridge.library import LibraryPublisher

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE + "/"
EMPHASIS = "\x86Sky \x87Cinema"


# ------------------------------------------------------------------ the helper --


def test_the_emphasis_codes_go():
    assert dvb_text(EMPHASIS) == "Sky Cinema"
    assert dvb_text("\x86TVP\x87 1 HD") == "TVP 1 HD"


def test_a_line_break_becomes_a_space():
    assert dvb_text("Serwis\x8ainformacyjny") == "Serwis informacyjny"


def test_every_code_of_the_range_goes_and_nothing_either_side_of_it():
    assert dvb_text("".join(chr(code) for code in range(0x80, 0xA0))) == " "
    kept = "\x7f\xa0\xe9\u0142\u20ac a"
    assert dvb_text(kept) == kept


@pytest.mark.parametrize("value", [None, 3, b"\x86x"])
def test_anything_but_text_is_left_as_it_is(value):
    assert dvb_text(value) == value


def test_the_resolver_name_is_clean():
    ServiceReference.names[TVP1] = "\x86TVP 1 HD\x87"
    assert enigma2_module.service_name(TVP1) == "TVP 1 HD"


# -------------------------------------------------------- the channel topics --


@pytest.fixture
def emphasised(receiver):
    """The image's names of two channels carry DVB's emphasis codes."""
    first = receiver.service_center.contents
    bouquet = [key for key, rows in first.items() if any(sref == TVP1 for sref, _ in rows)][0]
    first[bouquet] = [(sref, "\x86TVP 1\x87 HD" if sref == TVP1 else name)
                      for sref, name in first[bouquet]]
    ServiceReference.names[TVP1] = "\x86TVP 1\x87 HD"
    ServiceReference.names[TVN] = "TVN\x8aHD"
    return receiver


def _channel_names(factory):
    payload = factory.client.last(ROOT + "channels").json()
    return [channel["name"] for bouquet in payload["bouquets"] for channel in bouquet["channels"]]


def test_channels_carry_clean_names(emphasised, make_bridge, factory, settings):
    bridge = _live(make_bridge, factory, settings, emphasised)
    assert "TVP 1 HD" in _channel_names(factory)
    assert not any("\x86" in name or "\x87" in name for name in _channel_names(factory))
    lists = [entry for entry in factory.client.published
             if entry.topic.startswith(ROOT + "channels/") and entry.text]
    assert lists and not any("\\u0086" in entry.text or "\x86" in entry.text for entry in lists)
    assert bridge.publisher("channels").find_by_name("TVP 1 HD")[0] == TVP1


def test_a_zap_by_the_old_spelling_still_finds_the_channel(emphasised, make_bridge, factory,
                                                          settings):
    bridge = _live(make_bridge, factory, settings, emphasised)
    channels = bridge.publisher("channels")
    assert channels.find_by_name("\x86TVP 1\x87 HD") == (TVP1, None)
    assert channels.find_by_name("tvp 1 hd") == (TVP1, None)


def test_the_service_topic_names_the_channel_cleanly(emphasised, make_bridge, factory,
                                                     settings):
    _live(make_bridge, factory, settings, emphasised)
    assert factory.client.last(ROOT + "service").json()["name"] == "TVP 1 HD"


def test_the_image_name_used_when_the_resolver_has_none_is_clean(emphasised, make_bridge,
                                                                 factory, settings, monkeypatch):
    from MQTTBridge import service as service_module

    monkeypatch.setattr(service_module, "service_name", lambda sref: "")
    monkeypatch.setattr(emphasised.info, "name", "\x86Info\x87 name", raising=False)
    _live(make_bridge, factory, settings, emphasised)
    assert factory.client.last(ROOT + "service").json()["name"] == "Info name"


def _live(make_bridge, factory, settings, receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    return bridge


# ----------------------------------------------------------- recordings and timers --


def test_a_running_recording_and_its_timer_are_named_cleanly(emphasised, make_bridge, factory,
                                                             settings):
    bridge = _live(make_bridge, factory, settings, emphasised)
    timer = emphasised.add_timer(name="\x86Wiadomo\u015bci\x87",
                                 state=RecordTimerEntry.StateRunning)
    timer.description = "Serwis\x8a\x86informacyjny\x87"
    fire_coalesced(bridge)
    entry = factory.client.last(RECORDING).json()["active"][0]
    assert entry["name"] == "Wiadomo\u015bci"
    assert entry["service_name"] == "TVP 1 HD"
    assert entry["description"] == "Serwis informacyjny"
    fire_coalesced(bridge, "timers")
    assert factory.client.last(TIMERS).json()[0]["name"] == "Wiadomo\u015bci"


@pytest.fixture
def image(tmp_path, monkeypatch):
    image = PvrImage(tmp_path, monkeypatch).install()
    monkeypatch.setattr(LibraryPublisher, "run_in_background", staticmethod(inline))
    return image


def test_the_library_names_a_recording_and_its_channel_cleanly(image, emphasised, make_bridge,
                                                              factory, settings):
    image.record("a.ts", meta=meta_text(sref=TVP1, name="\x86Fakty\x87",
                                        description="Wydanie\x8a\x86wieczorne\x87"))
    image.record("20261017 2015 - \x86Polsat\x87 - \x86Sport\x87.ts")
    image.with_player(emphasised)
    _live(make_bridge, factory, settings, emphasised)
    payload = json.loads(factory.client.last(ROOT + "recordings/movie").text)
    by_file = {entry["file"]: entry for entry in payload["recordings"]}
    meta = by_file["a.ts"]
    assert (meta["name"], meta["description"], meta["service_name"]) == (
        "Fakty", "Wydanie wieczorne", "TVP 1 HD")
    named = by_file["20261017 2015 - \x86Polsat\x87 - \x86Sport\x87.ts"]
    # The file keeps its name - it is how the recording is found - and what is shown is clean.
    assert (named["name"], named["service_name"]) == ("Sport", "Polsat")


def test_the_cut_of_a_meta_line_is_clean():
    assert library._cut("\x86Fakty\x87 \x8a extra") == "Fakty extra"


def test_the_zap_history_name_from_the_image_is_clean(monkeypatch):
    from MQTTBridge import zaphistory

    class Info:
        def getName(self, reference):
            return "\x86Polsat\x87 Sport"

    class Center:
        def info(self, reference):
            return Info()

    monkeypatch.setattr(zaphistory, "service_name", lambda sref: "")
    name = zaphistory.ZapHistoryPublisher._name(Center(), object(), TVP1)
    assert name == "Polsat Sport"
