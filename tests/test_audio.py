"""The audio tracks of the playing service and their codecs.

The track stubs in `conftest.py` are `iAudioTrackInfo` as OpenViX 6.6 has it: a
description, a language and a PID, and nothing that names the codec. The words
below are the ones `eDVBServicePlay::getTrackInfo` writes in OpenViX, OpenATV
and OpenPLi.
"""

import pytest
from conftest import AudioTrackInfo, AudioTracks, Navigation, Service, Session

from MQTTBridge import audio


def session_with(tracks, current=0):
    service = Service(audio=AudioTracks(tracks, current))
    return Session(Navigation(service, "1:0:19:283D:3FB:1:C00000:0:0:0:"))


# --------------------------------------------------------------------- codecs --


@pytest.mark.parametrize(("description", "codec"), [
    # OpenATV and OpenPLi
    ("MPEG", "mp2"), ("AC3", "ac3"), ("AC3+", "eac3"), ("AC4", "ac4"), ("AAC", "aac"),
    ("AAC-HE", "aac_he"), ("DTS", "dts"), ("DTS-HD", "dts_hd"), ("LPCM", "lpcm"), ("DRA", "dra"),
    # OpenViX
    ("Dolby Digital", "ac3"), ("Dolby Digital +", "eac3"), ("Dolby Atmos", "eac3"),
    ("HE-AAC", "aac_he"), ("AAC-LC", "aac"), ("Dolby AC-4", "ac4"),
    # However it is cased and padded
    ("  mpeg ", "mp2"), ("dolby DIGITAL", "ac3"),
])
def test_an_images_word_for_a_track_is_a_stable_codec_name(description, codec):
    assert audio.codec_name(description) == codec


@pytest.mark.parametrize("description", [
    "???", "", "   ", "Dolby TrueHD", "Dolby Atmos (TrueHD)", "DTS-HD MA", "MP3", "Opus",
    "AC3 5.1", "MPEG-4", None, 7, b"MPEG",
])
def test_a_word_no_image_writes_for_a_dvb_track_is_an_unknown_codec(description):
    """Never guessed: a stream played through the media player uses other words."""
    assert audio.codec_name(description) is None


# --------------------------------------------------------------------- tracks --


def test_the_tracks_are_listed_in_the_images_order_with_the_active_one_marked():
    session = session_with([
        AudioTrackInfo("MPEG", "pol", 5001),
        AudioTrackInfo("Dolby Digital", "pol", 5002),
        AudioTrackInfo("AC3+", "eng", 5003),
    ], current=1)
    assert audio.read_tracks(session) == [
        {"index": 0, "description": "MPEG", "language": "pol", "pid": 5001, "codec": "mp2",
         "active": False},
        {"index": 1, "description": "Dolby Digital", "language": "pol", "pid": 5002,
         "codec": "ac3", "active": True},
        {"index": 2, "description": "AC3+", "language": "eng", "pid": 5003, "codec": "eac3",
         "active": False},
    ]
    assert audio.active_index(session) == 1
    assert audio.active_of(audio.read_tracks(session)) == 1


def test_the_first_track_is_an_index_like_any_other():
    session = session_with([AudioTrackInfo("MPEG"), AudioTrackInfo("AC3")], current=0)
    assert audio.active_index(session) == 0
    assert audio.active_of(audio.read_tracks(session)) == 0


def test_a_service_without_audio_has_an_empty_list():
    session = session_with([])
    assert audio.read_tracks(session) == []
    assert audio.active_index(session) is None
    assert audio.active_of([]) is None
    assert audio.active_of(None) is None


def test_nothing_playing_has_no_tracks():
    session = Session(Navigation(None, ""))
    assert audio.read_tracks(session) is None
    assert audio.read_tracks(None) is None
    assert audio.active_index(session) is None


def test_a_service_that_offers_no_track_selection_has_no_tracks():
    class Bare:
        pass

    assert audio.read_tracks(Session(Navigation(Bare(), "1:0:1:1:1:1:1:0:0:0:"))) is None


def test_a_track_selection_that_raises_is_no_tracks_and_no_traceback():
    class Broken:
        def audioTracks(self):
            raise RuntimeError("the service is going away")

    assert audio.read_tracks(Session(Navigation(Broken(), "1:0:1:1:1:1:1:0:0:0:"))) is None


def test_a_count_the_image_cannot_give_is_no_tracks():
    session = session_with([AudioTrackInfo()])
    session.nav.service._audio.raises = True
    assert audio.read_tracks(session) is None
    assert audio.active_index(session) is None


def test_a_negative_count_is_no_tracks():
    session = session_with([AudioTrackInfo()])
    session.nav.service._audio.getNumberOfTracks = lambda: -1
    assert audio.read_tracks(session) is None


def test_a_track_the_image_cannot_describe_is_still_listed():
    class Broken(AudioTracks):
        def getTrackInfo(self, index):
            if index == 0:
                raise RuntimeError("no program information")
            return AudioTracks.getTrackInfo(self, index)

    service = Service(audio=Broken([AudioTrackInfo(), AudioTrackInfo("AC3", "eng", 5002)], 1))
    tracks = audio.read_tracks(Session(Navigation(service, "1:0:1:1:1:1:1:0:0:0:")))
    assert tracks[0] == {"index": 0, "description": None, "language": None, "pid": None,
                         "codec": None, "active": False}
    assert tracks[1]["codec"] == "ac3" and tracks[1]["active"] is True


@pytest.mark.parametrize(("description", "language", "pid", "row"), [
    ("", "", -1, (None, None, None)),
    ("  ", None, None, (None, None, None)),
    (5, 6, "x", (None, None, None)),
    ("MPEG", "pol", 0, ("MPEG", "pol", 0)),
    ("D" * 100, "L" * 100, 8191, ("D" * 64, "L" * 64, 8191)),
])
def test_what_a_track_says_is_bounded_and_null_when_it_says_nothing(description, language, pid,
                                                                   row):
    track = audio.read_tracks(session_with([AudioTrackInfo(description, language, pid)]))[0]
    assert (track["description"], track["language"], track["pid"]) == row


def test_at_most_thirty_two_tracks_are_listed():
    tracks = [AudioTrackInfo("MPEG", "pol", 5000 + number) for number in range(40)]
    listed = audio.read_tracks(session_with(tracks, current=35))
    assert [track["index"] for track in listed] == list(range(32))
    assert audio.active_of(listed) is None


def test_a_current_track_the_image_cannot_name_marks_none():
    session = session_with([AudioTrackInfo()])

    def broken():
        raise RuntimeError("no decoder")

    session.nav.service._audio.getCurrentTrack = broken
    assert audio.read_tracks(session)[0]["active"] is False
    assert audio.active_index(session) is None


def test_a_negative_current_track_is_none():
    session = session_with([AudioTrackInfo()], current=-1)
    assert audio.active_index(session) is None
    assert audio.read_tracks(session)[0]["active"] is False
