"""The picture on `service` is the decoder's: its size, scan and frame rate.

Read from `/proc/stb/vmpeg/0` where the image has it and while the image's own
service information reports a picture, with that information answering for
what the decoder cannot - never one size from each, and never the image's scan
or frame rate beside a size the image does not agree with. The values written
here are the ones measured on OpenViX 6.6: `xres` and `yres` in hexadecimal
without a prefix, `progressive` 1 or 0, `framerate` in thousandths. And because
no event comes after an enigma2 restart, and the decoder is still settling for
a moment after a zap, `service` is read again a few seconds after the plugin
starts and after every service start.
"""

import pytest
from conftest import TVP1, MainLoop, iPlayableService

from MQTTBridge import service as service_module

NODE = "vuuno4kse_005301"
SERVICE = "enigma2/" + NODE + "/service"

EV_START = iPlayableService.evStart
EV_UPDATED_INFO = iPlayableService.evUpdatedInfo
EV_SIZE = iPlayableService.evVideoSizeChanged

# What `/proc/stb/vmpeg/0` said for three channels.
P720_50 = {"xres": "500", "yres": "2d0", "progressive": "1", "framerate": "50000"}
I1080_25 = {"xres": "780", "yres": "438", "progressive": "0", "framerate": "25000"}
I576_25 = {"xres": "2d0", "yres": "240", "progressive": "0", "framerate": "25000"}

# What `service` makes of them, and what the fake image says by default.
DECODED = (1280, 720, False, 50.0)
DECODED_1080 = (1920, 1080, True, 25.0)
IMAGE = (1920, 1080, True, 25.0)
STALE = (1920, 1080, False, 50.0)
NOTHING = (None, None, None, None)

AUDIO_FILE = "4097:0:0:0:0:0:0:0:0:0:/media/hdd/music/track.mp3"


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


def no_video(receiver):
    """What the image says for a service without a picture: radio, or one not decoding yet."""
    receiver.info.width = receiver.info.height = -1
    receiver.info.progressive = receiver.info.frame_rate = -1
    receiver.info.video_type = -1


def image_720p50(receiver):
    receiver.info.width, receiver.info.height = 1280, 720
    receiver.info.progressive, receiver.info.frame_rate = 1, 50000


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
    assert picture(payload) == DECODED
    assert (payload["sref"], payload["video_codec"]) == (TVP1, "h264")


@pytest.mark.parametrize(("values", "expected"), [
    (P720_50, DECODED), (I1080_25, DECODED_1080), (I576_25, (720, 576, True, 25.0)),
])
def test_the_measured_channels_read_as_they_are(make_bridge, factory, settings, receiver,
                                                decoder, values, expected):
    stale_image(receiver)
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
    stale_image(receiver)
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
    assert picture(published(factory)) == NOTHING


# --------------------------------------------- what the decoder cannot answer --

BAD = [None, "", "zz", "-1", "1" * 16, "\u00e9",
       # 500 in Arabic-Indic digits: a number to Python's `int`, not one a driver writes.
       "\u0665\u0660\u0660"]


@pytest.mark.parametrize("name", ["xres", "yres"])
@pytest.mark.parametrize("text", BAD)
def test_the_size_is_a_pair_and_half_of_one_is_the_images_picture(
    make_bridge, factory, settings, receiver, decoder, name, text
):
    """Never 1280 x 1080: a decoder that cannot give both sizes leaves all four to the image."""
    decoder.write(dict(P720_50, **{name: text}))
    start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == IMAGE


# A scan or a frame rate the decoder cannot give: the bad texts, and a value out of its range.
UNGIVEN = ([("progressive", 2, text) for text in [*BAD, "2"]]
           + [("framerate", 3, text) for text in [*BAD, "0"]])


@pytest.mark.parametrize(("name", "member", "text"), UNGIVEN)
def test_beside_a_size_the_image_disagrees_with_a_missing_scan_or_rate_is_null(
    make_bridge, factory, settings, receiver, decoder, name, member, text
):
    """The image describes another picture then: its 1080i beside the decoder's 720 is no "720i"."""
    decoder.write(dict(P720_50, **{name: text}))
    start(make_bridge, factory, settings, receiver)
    expected = list(DECODED)
    expected[member] = None
    assert picture(published(factory)) == tuple(expected)


@pytest.mark.parametrize(("name", "member", "text"), UNGIVEN)
def test_beside_a_size_the_image_agrees_with_a_missing_scan_or_rate_is_the_images(
    make_bridge, factory, settings, receiver, decoder, name, member, text
):
    stale_image(receiver)
    decoder.write(dict(I1080_25, **{name: text}))
    start(make_bridge, factory, settings, receiver)
    expected = list(DECODED_1080)
    expected[member] = STALE[member]
    assert picture(published(factory)) == tuple(expected)


@pytest.mark.parametrize(("xres", "yres", "size"), [
    ("780", "320", (1920, 800)), ("5a0", "438", (1440, 1080)),
])
def test_a_size_that_agrees_in_one_direction_only_is_another_picture(
    make_bridge, factory, settings, receiver, decoder, xres, yres, size
):
    """Both sizes have to agree before the image may answer for the scan."""
    decoder.write(dict(P720_50, xres=xres, yres=yres, progressive=None))
    start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == (*size, None, 50.0)


@pytest.mark.parametrize(("width", "height"), [(1920, -1), (-1, 1080)])
def test_an_image_that_reports_half_a_size_has_the_decoder_left_out(
    make_bridge, factory, settings, receiver, decoder, width, height
):
    """Not a picture by the image's word either: the files may be the last channel's."""
    receiver.info.width, receiver.info.height = width, height
    decoder.write(P720_50)
    start(make_bridge, factory, settings, receiver)
    expected = (None if width < 0 else width, None if height < 0 else height, True, 25.0)
    assert picture(published(factory)) == expected


@pytest.mark.parametrize("name", ["xres", "yres", "progressive", "framerate"])
def test_a_file_that_cannot_be_read_counts_as_missing(make_bridge, factory, settings, receiver,
                                                      decoder, name):
    decoder.write(dict(P720_50, **{name: None}))
    (decoder.path / name).mkdir()
    start(make_bridge, factory, settings, receiver)
    expected = {"xres": IMAGE, "yres": IMAGE, "progressive": (1280, 720, None, 50.0),
                "framerate": (1280, 720, False, None)}[name]
    assert picture(published(factory)) == expected


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


# ----------------------------------------------- a service without a picture --


def test_radio_at_start_does_not_get_the_last_channels_picture(make_bridge, factory, settings,
                                                               receiver, decoder):
    """The files keep the last television channel's picture; the image says there is none."""
    no_video(receiver)
    decoder.write(P720_50)
    start(make_bridge, factory, settings, receiver)
    payload = published(factory)
    assert (picture(payload), payload["video_codec"]) == (NOTHING, None)
    MainLoop.advance(10 * 1000)
    assert picture(published(factory)) == NOTHING


def test_a_zap_to_radio_stays_without_a_picture_through_both_re_reads(
    make_bridge, factory, settings, receiver, decoder
):
    stale_image(receiver)
    decoder.write(P720_50)
    bridge = start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == DECODED
    no_video(receiver)
    receiver.nav.fire(EV_START)
    assert picture(published(factory)) == NOTHING
    reread(bridge).timer.fire()
    assert picture(published(factory)) == NOTHING
    reread(bridge).timer.fire()
    assert picture(published(factory)) == NOTHING


def test_a_tune_slower_than_the_re_reads_never_shows_the_last_channel(
    make_bridge, factory, settings, receiver, decoder
):
    """A moving dish, a key that has not come, a channel that never decodes."""
    decoder.write(I1080_25)
    start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == DECODED_1080
    no_video(receiver)
    receiver.nav.fire(EV_START)
    MainLoop.advance(10 * 1000)
    assert picture(published(factory)) == NOTHING
    image_720p50(receiver)
    decoder.write(P720_50)
    receiver.nav.fire(EV_SIZE)
    assert picture(published(factory)) == DECODED


def test_an_audio_file_has_no_picture(make_bridge, factory, settings, receiver, decoder):
    stale_image(receiver)
    decoder.write(P720_50)
    bridge = start(make_bridge, factory, settings, receiver)
    receiver.nav.sref = AUDIO_FILE
    no_video(receiver)
    receiver.nav.fire(EV_START)
    reread(bridge).timer.fire()
    reread(bridge).timer.fire()
    payload = published(factory)
    assert (payload["sref"], picture(payload)) == (AUDIO_FILE, NOTHING)


# ------------------------------------------------------------- the re-reads --


def test_the_plugin_reads_again_three_and_eight_seconds_after_it_starts(
    make_bridge, factory, settings, receiver, decoder
):
    """Nothing announces the picture after an enigma2 restart; the re-reads find it."""
    stale_image(receiver)
    bridge = start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == STALE
    assert reread(bridge).timer.started == (3000, True)
    decoder.write(P720_50)
    reread(bridge).timer.fire()
    assert picture(published(factory)) == DECODED
    assert reread(bridge).timer.started == (5000, True)
    decoder.write(I1080_25)
    reread(bridge).timer.fire()
    assert picture(published(factory)) == DECODED_1080


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
    stale_image(receiver)
    reread(bridge).timer.fire()
    assert picture(published(factory)) == DECODED


# ------------------------------------------- the moment right after a zap --


def test_right_after_a_start_the_decoders_left_over_picture_is_not_read(
    make_bridge, factory, settings, receiver, decoder
):
    """The files still hold the last channel's picture; the image's answer goes out instead."""
    decoder.write(P720_50)
    start(make_bridge, factory, settings, receiver)
    assert picture(published(factory)) == DECODED
    receiver.nav.fire(EV_START)
    assert picture(published(factory)) == IMAGE
    receiver.nav.fire(EV_UPDATED_INFO)
    assert picture(published(factory)) == IMAGE


@pytest.mark.parametrize("event", ["evVideoSizeChanged", "evVideoFramerateChanged",
                                   "evVideoProgressiveChanged"])
def test_the_decoders_own_event_says_its_files_are_the_new_picture(
    make_bridge, factory, settings, receiver, decoder, event
):
    decoder.write(P720_50)
    start(make_bridge, factory, settings, receiver)
    receiver.nav.fire(EV_START)
    assert picture(published(factory)) == IMAGE
    decoder.write(I576_25)
    receiver.nav.fire(getattr(iPlayableService, event))
    assert picture(published(factory)) == (720, 576, True, 25.0)
    receiver.nav.fire(EV_UPDATED_INFO)
    assert picture(published(factory)) == (720, 576, True, 25.0)


def test_the_first_re_read_ends_the_hold(make_bridge, factory, settings, receiver, decoder):
    decoder.write(P720_50)
    bridge = start(make_bridge, factory, settings, receiver)
    receiver.nav.fire(EV_START)
    decoder.write(I576_25)
    reread(bridge).timer.fire()
    assert picture(published(factory)) == (720, 576, True, 25.0)
    receiver.nav.fire(EV_UPDATED_INFO)
    assert picture(published(factory)) == (720, 576, True, 25.0)


def test_before_any_start_the_decoder_is_read_at_once(make_bridge, factory, settings, receiver,
                                                      decoder):
    """The plugin starting is not a zap: the picture on the screen is the one in the files."""
    stale_image(receiver)
    decoder.write(P720_50)
    bridge = start(make_bridge, factory, settings, receiver)
    receiver.nav.fire(EV_UPDATED_INFO)
    assert picture(published(factory)) == DECODED
    assert bridge.publisher("service")._decoder_held is False


def test_a_publisher_started_again_does_not_keep_an_old_hold(make_bridge, factory, settings,
                                                             receiver, decoder):
    """A reload between a zap and its first re-read: the new start has seen no zap."""
    stale_image(receiver)
    decoder.write(P720_50)
    bridge = start(make_bridge, factory, settings, receiver)
    publisher = bridge.publisher("service")
    receiver.nav.fire(EV_START)
    assert picture(published(factory)) == STALE
    publisher.stop()
    assert publisher.start()
    assert picture(publisher.snapshot()["service"]) == DECODED
