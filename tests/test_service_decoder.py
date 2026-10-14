"""The picture on `service` is the decoder's: its size, scan and frame rate.

Read from `/proc/stb/vmpeg/0` where the image has it, member by member, with
the image's service information answering for what the decoder cannot. The
values written here are the ones measured on OpenViX 6.6: `xres` and `yres` in
hexadecimal without a prefix, `progressive` 1 or 0, `framerate` in thousandths.
And because no event comes after an enigma2 restart, and the decoder is still
settling for a moment after a zap, `service` is read again a few seconds after
the plugin starts and after every service start.
"""

import pytest
from conftest import TVP1, MainLoop, iPlayableService

from MQTTBridge import service as service_module

NODE = "vuuno4kse_005301"
SERVICE = "enigma2/" + NODE + "/service"

EV_START = iPlayableService.evStart
EV_UPDATED_INFO = iPlayableService.evUpdatedInfo

# What `/proc/stb/vmpeg/0` said for three channels, and what `service` makes of it.
P720_50 = {"xres": "500", "yres": "2d0", "progressive": "1", "framerate": "50000"}
I1080_25 = {"xres": "780", "yres": "438", "progressive": "0", "framerate": "25000"}
I576_25 = {"xres": "2d0", "yres": "240", "progressive": "0", "framerate": "25000"}


class Decoder:
    """`/proc/stb/vmpeg/0` in a temporary directory: one file per value, as the driver has it."""

    def __init__(self, path):
        self.path = path

    def write(self, values):
        self.path.mkdir(exist_ok=True)
        for name, text in values.items():
            target = self.path / name
            if text is None:
                if target.exists():
                    target.unlink()
                continue
            target.write_bytes((text + "\n").encode("utf-8"))


@pytest.fixture
def decoder(tmp_path, monkeypatch):
    path = tmp_path / "vmpeg0"
    monkeypatch.setattr(service_module, "DECODER_PATH", str(path))
    return Decoder(path)


def picture(payload):
    return (payload["width"], payload["height"], payload["interlaced"], payload["frame_rate"])


def published(factory):
    return factory.client.last(SERVICE).json()


def start(make_bridge, factory, settings, receiver):
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    return bridge


def stale_image(receiver):
    """What the image's service information said for a 720p50 channel after an enigma2 restart."""
    receiver.info.width, receiver.info.height = 1920, 1080
    receiver.info.progressive, receiver.info.frame_rate = 1, 50000


def blank_image(receiver):
    """An image whose service information knows nothing of the picture."""
    receiver.info.width = receiver.info.height = -1
    receiver.info.progressive = receiver.info.frame_rate = -1


def reread(bridge):
    return bridge.publisher("service")._reread


# ------------------------------------------------------------------ the values --


def test_the_decoder_wins_over_the_images_stale_picture(make_bridge, factory, settings,
                                                        receiver, decoder):
    """The case this is for: enigma2 restarted, the image says 1080, the screen shows 720p50."""
    stale_image(receiver)
    decoder.write(P720_50)
    start(make_bridge, factory, settings, receiver)
    payload = published(factory)
    assert picture(payload) == (1280, 720, False, 50.0)
    assert (payload["sref"], payload["video_codec"]) == (TVP1, "h264")


@pytest.mark.parametrize(("values", "expected"), [
    (P720_50, (1280, 720, False, 50.0)),
    (I1080_25, (1920, 1080, True, 25.0)),
    (I576_25, (720, 576, True, 25.0)),
])
def test_the_measured_channels_read_as_they_are(make_bridge, factory, settings, receiver,
                                                decoder, values, expected):
    blank_image(receiver)
    decoder.write(values)
    start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == expected


@pytest.mark.parametrize(("xres", "yres", "size"), [
    ("500", "2d0", (1280, 720)), ("2D0", "240", (720, 576)), ("0x780", "0x438", (1920, 1080)),
    ("f00", "870", (3840, 2160)), ("  780\t", "438", (1920, 1080)),
])
def test_the_size_is_hexadecimal(make_bridge, factory, settings, receiver, decoder,
                                 xres, yres, size):
    """`500` is 1280, never five hundred: decimal would make 720p a picture no channel sends."""
    blank_image(receiver)
    decoder.write(dict(P720_50, xres=xres, yres=yres))
    start(make_bridge, factory, settings, receiver)
    payload = published(factory)
    assert (payload["width"], payload["height"]) == size


@pytest.mark.parametrize(("reported", "rate"), [
    ("25000", 25.0), ("50000", 50.0), ("23976", 23.976), ("29970", 29.97), ("59940", 59.94),
    ("1", 0.001),
])
def test_the_frame_rate_is_the_decoders_thousandths(make_bridge, factory, settings, receiver,
                                                    decoder, reported, rate):
    blank_image(receiver)
    decoder.write(dict(P720_50, framerate=reported))
    start(make_bridge, factory, settings, receiver)
    assert published(factory)["frame_rate"] == rate


@pytest.mark.parametrize("zero", ["xres", "yres"])
def test_a_decoder_without_a_picture_yet_makes_all_four_null(make_bridge, factory, settings,
                                                             receiver, decoder, zero):
    """`0` is "not known yet", and its scan and frame rate then mean nothing: null, never 0."""
    stale_image(receiver)
    decoder.write(dict(P720_50, **{zero: "0"}))
    start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == (None, None, None, None)


# --------------------------------------------- what the decoder cannot answer --

MEMBER_OF = {"xres": 0, "yres": 1, "progressive": 2, "framerate": 3}
IMAGE = (1920, 1080, True, 25.0)
DECODED = (1280, 720, False, 50.0)


def mixed(name):
    """The 720p50 picture, with the one member `name` feeds taken from the image."""
    expected = list(DECODED)
    expected[MEMBER_OF[name]] = IMAGE[MEMBER_OF[name]]
    return tuple(expected)


@pytest.mark.parametrize("name", list(MEMBER_OF))
# The last is 500 in Arabic-Indic digits: a number to Python's `int`, not one a driver writes.
@pytest.mark.parametrize("text", [None, "", "zz", "-1", "1" * 16, "\u00e9",
                                  "\u0665\u0660\u0660"])
def test_a_file_that_is_missing_or_not_a_number_is_answered_by_the_image(
    make_bridge, factory, settings, receiver, decoder, name, text
):
    """Value by value: one bad file costs that member the decoder's word, not the others."""
    decoder.write(dict(P720_50, **{name: text}))
    start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == mixed(name)


@pytest.mark.parametrize("name", list(MEMBER_OF))
def test_a_file_that_cannot_be_read_is_answered_by_the_image(make_bridge, factory, settings,
                                                             receiver, decoder, name):
    decoder.write(dict(P720_50, **{name: None}))
    (decoder.path / name).mkdir()
    start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == mixed(name)


@pytest.mark.parametrize(("name", "text"), [("progressive", "2"), ("framerate", "0")])
def test_a_scan_or_rate_the_decoder_cannot_have_is_answered_by_the_image(
    make_bridge, factory, settings, receiver, decoder, name, text
):
    decoder.write(dict(P720_50, **{name: text}))
    start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == mixed(name)


def test_sixteen_bytes_are_still_read(make_bridge, factory, settings, receiver, decoder):
    """The bound is far above anything the driver writes: sixteen bytes, the newline included."""
    decoder.write(dict(P720_50, xres="0" * 12 + "500"))
    start(make_bridge, factory, settings, receiver)
    assert published(factory)["width"] == 1280


def test_without_the_decoders_files_the_image_answers_as_before(make_bridge, factory, settings,
                                                                receiver):
    """A box with no `/proc/stb/vmpeg/0`: the image's service information, unchanged."""
    start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == IMAGE


def test_the_codec_is_still_the_images(make_bridge, factory, settings, receiver, decoder):
    decoder.write(dict(P720_50, codec="7"))
    receiver.info.video_type = 0
    start(make_bridge, factory, settings, receiver)
    payload = published(factory)
    assert (payload["video_codec"], payload["width"]) == ("mpeg2", 1280)


# ------------------------------------------------------------- the re-reads --


def test_the_plugin_reads_again_three_and_eight_seconds_after_it_starts(
    make_bridge, factory, settings, receiver, decoder
):
    """Nothing announces the picture after an enigma2 restart; the re-reads find it."""
    stale_image(receiver)
    bridge = start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == (1920, 1080, False, 50.0)
    assert reread(bridge).timer.started == (3000, True)
    decoder.write(P720_50)
    reread(bridge).timer.fire()
    assert picture(published(factory)) == DECODED
    assert reread(bridge).timer.started == (5000, True)
    decoder.write(I1080_25)
    reread(bridge).timer.fire()
    assert picture(published(factory)) == (1920, 1080, True, 25.0)


def test_the_re_reads_end_after_the_second(make_bridge, factory, settings, receiver, decoder,
                                           monkeypatch):
    """Two reads at three and eight seconds on the clock, then none: not a poll."""
    decoder.write(P720_50)
    bridge = start(make_bridge, factory, settings, receiver)
    reads = []
    real = service_module.read_decoder
    monkeypatch.setattr(service_module, "read_decoder",
                        lambda: reads.append(MainLoop.now) or real())
    MainLoop.advance(30 * 1000)
    # The audio look does not read the service when the track did not move.
    assert reads == [3000, 8000]
    assert not reread(bridge).timer.running


def test_a_re_read_that_finds_nothing_new_publishes_nothing(make_bridge, factory, settings,
                                                            receiver, decoder):
    decoder.write(P720_50)
    bridge = start(make_bridge, factory, settings, receiver)
    factory.client.clear()
    reread(bridge).timer.fire()
    reread(bridge).timer.fire()
    assert factory.client.all_for(SERVICE) == []


def test_a_service_start_starts_the_re_reads_over(make_bridge, factory, settings, receiver,
                                                  decoder):
    decoder.write(P720_50)
    bridge = start(make_bridge, factory, settings, receiver)
    reread(bridge).timer.fire()
    assert reread(bridge).timer.started == (5000, True)
    receiver.nav.fire(EV_START)
    assert reread(bridge).timer.started == (3000, True)
    assert reread(bridge).timer.running
    reread(bridge).timer.fire()
    assert reread(bridge).timer.started == (5000, True)


def test_stopping_the_publisher_drops_the_pending_re_read(make_bridge, factory, settings,
                                                          receiver, decoder):
    bridge = start(make_bridge, factory, settings, receiver)
    timer = reread(bridge).timer
    assert timer.running
    bridge.publisher("service").stop()
    assert not timer.running
    assert reread(bridge).timer is None


def test_a_zap_publishes_the_decoders_picture_at_the_first_re_read(
    make_bridge, factory, settings, receiver, decoder
):
    """A zap between two channels of the same size may bring no size event at all."""
    decoder.write(I1080_25)
    bridge = start(make_bridge, factory, settings, receiver)
    receiver.nav.fire(EV_START)
    decoder.write(P720_50)
    blank_image(receiver)
    reread(bridge).timer.fire()
    assert picture(published(factory)) == DECODED


# ------------------------------------------- the moment right after a zap --


def test_right_after_a_start_the_decoders_left_over_picture_is_not_read(
    make_bridge, factory, settings, receiver, decoder
):
    """The files still hold the last channel's picture; the image's answer goes out instead."""
    blank_image(receiver)
    decoder.write(I1080_25)
    start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == (1920, 1080, True, 25.0)
    receiver.nav.fire(EV_START)
    assert picture(published(factory)) == (None, None, None, None)
    receiver.nav.fire(EV_UPDATED_INFO)
    assert picture(published(factory)) == (None, None, None, None)


@pytest.mark.parametrize("event", ["evVideoSizeChanged", "evVideoFramerateChanged",
                                   "evVideoProgressiveChanged"])
def test_the_decoders_own_event_says_its_files_are_the_new_picture(
    make_bridge, factory, settings, receiver, decoder, event
):
    blank_image(receiver)
    decoder.write(I1080_25)
    start(make_bridge, factory, settings, receiver)
    receiver.nav.fire(EV_START)
    assert picture(published(factory)) == (None, None, None, None)
    decoder.write(P720_50)
    receiver.nav.fire(getattr(iPlayableService, event))
    assert picture(published(factory)) == DECODED
    receiver.nav.fire(EV_UPDATED_INFO)
    assert picture(published(factory)) == DECODED


def test_the_first_re_read_ends_the_hold(make_bridge, factory, settings, receiver, decoder):
    decoder.write(I1080_25)
    bridge = start(make_bridge, factory, settings, receiver)
    blank_image(receiver)
    receiver.nav.fire(EV_START)
    decoder.write(P720_50)
    reread(bridge).timer.fire()
    assert picture(published(factory)) == DECODED
    receiver.nav.fire(EV_UPDATED_INFO)
    assert picture(published(factory)) == DECODED


def test_before_any_start_the_decoder_is_read_at_once(make_bridge, factory, settings, receiver,
                                                      decoder):
    """The plugin starting is not a zap: the picture on the screen is the one in the files."""
    stale_image(receiver)
    decoder.write(P720_50)
    bridge = start(make_bridge, factory, settings, receiver)
    receiver.nav.fire(EV_UPDATED_INFO)
    assert picture(published(factory)) == DECODED
    assert bridge.publisher("service")._decoder_held is False
