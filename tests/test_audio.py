"""The audio tracks of the playing service, their codecs, and whether the volume acts on them.

The track stubs in `conftest.py` are `iAudioTrackInfo` as OpenViX 6.6 has it: a
description, a language and a PID, and nothing that names the codec. The words
below are the ones `eDVBServicePlay::getTrackInfo` writes in OpenViX, OpenATV
and OpenPLi.
"""

import pytest
from Components.config import ConfigSelection, ConfigSubsection, ConfigYesNo, config
from conftest import AudioTrackInfo, AudioTracks, Navigation, Service, Session

from MQTTBridge import audio

WORDS = ("downmix", "passthrough")


def active_of(tracks):
    """The index of the row marked active, or None."""
    for track in tracks or ():
        if track["active"]:
            return track["index"]
    return None


def session_with(tracks, current=0):
    service = Service(audio=AudioTracks(tracks, current))
    return Session(Navigation(service, "1:0:19:283D:3FB:1:C00000:0:0:0:"))


@pytest.fixture
def av(monkeypatch):
    """`config.av`, empty: a test adds the elements its image would have."""
    section = ConfigSubsection()
    monkeypatch.setattr(config, "av", section, raising=False)
    return section


def word(value, choices=WORDS):
    return ConfigSelection(choices=list(choices), default=value)


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
    assert active_of(audio.read_tracks(session)) == 1


def test_the_first_track_is_an_index_like_any_other():
    session = session_with([AudioTrackInfo("MPEG"), AudioTrackInfo("AC3")], current=0)
    assert audio.active_index(session) == 0
    assert active_of(audio.read_tracks(session)) == 0


def test_a_service_without_audio_has_an_empty_list():
    session = session_with([])
    assert audio.read_tracks(session) == []
    assert audio.active_index(session) is None
    assert active_of([]) is None
    assert active_of(None) is None


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
    assert active_of(listed) is None


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


# ---------------------------------------------------------------- passthrough --


def active(description):
    return audio.read_tracks(session_with([AudioTrackInfo("MPEG"), AudioTrackInfo(description)],
                                          current=1))


@pytest.mark.parametrize(("description", "element", "value", "expected"), [
    # OpenViX and OpenPLi keep a word.
    ("Dolby Digital", "downmix_ac3", "passthrough", True),
    ("Dolby Digital", "downmix_ac3", "downmix", False),
    ("AC3", "downmix_ac3", "passthrough", True),
    ("AAC-LC", "downmix_aac", "passthrough", True),
    ("AAC", "downmix_aac", "downmix", False),
    ("DTS", "downmix_dts", "passthrough", True),
    ("DTS", "downmix_dts", "downmix", False),
    ("Dolby Digital +", "downmix_ac3plus", "passthrough", True),
    ("Dolby Digital +", "downmix_ac3plus", "downmix", False),
    ("AC3+", "transcodeac3plus", "passthrough", True),
    ("AC3+", "transcodeac3plus", "downmix", False),
    ("HE-AAC", "downmix_aacplus", "passthrough", True),
    ("AAC-HE", "downmix_aacplus", "downmix", False),
    ("DTS-HD", "dtshd", "passthrough", True),
    ("DTS-HD", "dtshd", "downmix", False),
])
def test_the_active_tracks_codec_and_its_setting_decide(av, description, element, value,
                                                        expected):
    setattr(av, element, word(value))
    assert audio.passthrough_of(active(description)) is expected


@pytest.mark.parametrize(("description", "element"), [
    ("AC3", "downmix_ac3"), ("DTS", "downmix_dts"), ("AAC", "downmix_aac"),
])
def test_a_boolean_setting_means_downmix_when_it_is_true(av, description, element):
    """OpenATV keeps a yes/no on most receivers, and yes is "downmix"."""
    setattr(av, element, ConfigYesNo(default=True))
    assert audio.passthrough_of(active(description)) is False
    getattr(av, element).value = False
    assert audio.passthrough_of(active(description)) is True


@pytest.mark.parametrize("value", ["multichannel", "hdmi_best", "use_hdmi_caps", "force_ac3",
                                   "force_dts", "0", "1", "2"])
def test_a_choice_that_is_neither_of_the_two_is_unknown(av, value):
    """What the television is offered then depends on things this plugin cannot see."""
    av.downmix_ac3 = word(value, choices=(value,))
    av.transcodeac3plus = word(value, choices=(value,))
    assert audio.passthrough_of(active("AC3")) is None
    assert audio.passthrough_of(active("AC3+")) is None


def test_a_setting_spelled_with_capitals_and_spaces_is_read(av):
    av.downmix_ac3 = type("Element", (), {"value": " Passthrough "})()
    assert audio.passthrough_of(active("AC3")) is True


@pytest.mark.parametrize("description", ["MPEG", "LPCM"])
def test_a_codec_the_receiver_always_decodes_is_never_passed_through(description):
    """With no `config.av` at all: nothing has to be read to know it."""
    assert audio.passthrough_of(active(description)) is False


@pytest.mark.parametrize("description", ["AC3", "AC3+", "AAC", "HE-AAC", "DTS", "DTS-HD"])
def test_an_image_without_the_setting_cannot_tell(description):
    assert audio.passthrough_of(active(description)) is None


def test_an_image_whose_av_section_lacks_the_element_cannot_tell(av):
    av.downmix_dts = word("passthrough")
    assert audio.passthrough_of(active("AC3")) is None


@pytest.mark.parametrize("description", ["???", "DRA", "AC4", "Dolby AC-4", "Dolby TrueHD", ""])
def test_a_codec_without_a_known_setting_cannot_be_told(av, description):
    av.downmix_ac3 = word("passthrough")
    av.downmix_aac = word("passthrough")
    assert audio.passthrough_of(active(description)) is None


def test_he_aac_is_not_judged_by_the_setting_for_plain_aac(av):
    """A receiver has a setting of its own for it or none; which one governs it is not known."""
    av.downmix_aac = word("passthrough")
    assert audio.passthrough_of(active("HE-AAC")) is None


def test_the_placeholder_an_image_puts_where_it_cannot_is_no_setting(av):
    """`ConfigNothing` holds the empty string: skipped, and the next name is asked."""
    av.downmix_ac3plus = type("Element", (), {"value": ""})()
    av.transcodeac3plus = word("passthrough")
    assert audio.passthrough_of(active("AC3+")) is True
    av.downmix_ac3 = type("Element", (), {"value": ""})()
    assert audio.passthrough_of(active("AC3")) is None


def test_a_setting_of_nothing_but_spaces_is_no_setting(av):
    av.downmix_ac3plus = type("Element", (), {"value": "   "})()
    av.transcodeac3plus = word("passthrough")
    assert audio.passthrough_of(active("AC3+")) is True


def test_the_first_setting_an_image_has_decides(av):
    av.downmix_ac3plus = word("downmix")
    av.transcodeac3plus = word("passthrough")
    assert audio.passthrough_of(active("AC3+")) is False


@pytest.mark.parametrize("value", [None, 5, 1.0, ["passthrough"]])
def test_a_setting_of_another_type_is_no_setting(av, value):
    av.downmix_ac3 = type("Element", (), {"value": value})()
    assert audio.passthrough_of(active("AC3")) is None


def test_a_setting_that_raises_is_no_setting(av):
    class Broken:
        @property
        def value(self):
            raise RuntimeError("not loaded")

    av.downmix_ac3 = Broken()
    assert audio.passthrough_of(active("AC3")) is None


def test_only_the_active_track_counts(av):
    av.downmix_ac3 = word("passthrough")
    tracks = audio.read_tracks(session_with(
        [AudioTrackInfo("MPEG"), AudioTrackInfo("AC3")], current=0))
    assert audio.passthrough_of(tracks) is False
    tracks = audio.read_tracks(session_with(
        [AudioTrackInfo("AC3"), AudioTrackInfo("MPEG")], current=0))
    assert audio.passthrough_of(tracks) is True


@pytest.mark.parametrize("tracks", [None, [], [{"index": 0, "codec": "ac3", "active": False}]])
def test_without_an_active_track_it_cannot_be_told(av, tracks):
    av.downmix_ac3 = word("passthrough")
    assert audio.passthrough_of(tracks) is None


def test_passthrough_reads_what_is_playing_now(av):
    av.downmix_ac3 = word("passthrough")
    session = session_with([AudioTrackInfo("MPEG"), AudioTrackInfo("AC3")], current=0)
    track = audio.ActiveTrack()
    assert audio.passthrough_for(track.codec(session)) is False
    session.nav.service._audio.selectTrack(1)
    assert audio.passthrough_for(track.codec(session)) is True
    assert audio.passthrough_for(track.codec(None)) is None


def counted(session):
    """Every `getTrackInfo` the session's service is asked for from here on."""
    asked = []
    tracks = session.nav.service._audio
    real = tracks.getTrackInfo
    tracks.getTrackInfo = lambda index: asked.append(index) or real(index)
    return asked


def test_the_active_tracks_codec_costs_one_track_and_then_none():
    """Not every track: on one image describing a track starts work in the decoder."""
    session = session_with([AudioTrackInfo("MPEG")] * 31 + [AudioTrackInfo("AC3")], current=31)
    asked = counted(session)
    track = audio.ActiveTrack()
    assert track.codec(session) == "ac3"
    assert asked == [31]
    assert [track.codec(session) for _ in range(5)] == ["ac3"] * 5
    assert asked == [31]


def test_another_track_or_another_service_is_read_once(av):
    session = session_with([AudioTrackInfo("MPEG"), AudioTrackInfo("AC3")], current=0)
    asked = counted(session)
    track = audio.ActiveTrack()
    assert track.codec(session) == "mp2"
    session.nav.service._audio.selectTrack(1)
    assert track.codec(session) == "ac3"
    assert track.codec(session) == "ac3"
    assert asked == [0, 1]
    session.nav.sref = "1:0:19:283E:3FB:1:C00000:0:0:0:"
    session.nav.service._audio.tracks[1] = AudioTrackInfo("DTS")
    assert track.codec(session) == "dts"
    assert asked == [0, 1, 1]


def test_a_forgotten_track_is_read_again():
    """After a service event the same index may be another track."""
    session = session_with([AudioTrackInfo("AC3")])
    asked = counted(session)
    track = audio.ActiveTrack()
    assert track.codec(session) == "ac3"
    session.nav.service._audio.tracks[0] = AudioTrackInfo("AAC")
    assert track.codec(session) == "ac3"
    track.forget()
    assert track.codec(session) == "aac"
    assert asked == [0, 0]


def test_a_track_is_read_again_after_so_many_looks_and_not_before():
    """A bound on how long a track that changed under the same index can go unnoticed."""
    session = session_with([AudioTrackInfo("AC3")])
    asked = counted(session)
    track = audio.ActiveTrack(reread_looks=6)
    assert track.codec(session) == "ac3"
    session.nav.service._audio.tracks[0] = AudioTrackInfo("MPEG")
    for _ in range(5):
        track.looked()
        assert track.codec(session) == "ac3"
    assert asked == [0]
    track.looked()
    assert track.codec(session) == "mp2"
    assert asked == [0, 0]
    for _ in range(5):
        track.looked()
        track.codec(session)
    assert asked == [0, 0]
    track.looked()
    track.codec(session)
    assert asked == [0, 0, 0]


def test_asking_between_looks_does_not_bring_the_next_read_nearer():
    """A volume key press asks; only the periodic look counts towards the bound."""
    session = session_with([AudioTrackInfo("AC3")])
    asked = counted(session)
    track = audio.ActiveTrack(reread_looks=2)
    for _ in range(50):
        track.codec(session)
    assert asked == [0]
    track.looked()
    track.codec(session)
    assert asked == [0]
    track.looked()
    track.codec(session)
    assert asked == [0, 0]


def test_a_bound_of_one_look_reads_at_every_look():
    session = session_with([AudioTrackInfo("AC3")])
    asked = counted(session)
    track = audio.ActiveTrack(reread_looks=1)
    for _ in range(3):
        track.looked()
        track.codec(session)
    assert asked == [0, 0, 0]


def test_without_a_bound_a_look_changes_nothing():
    session = session_with([AudioTrackInfo("AC3")])
    asked = counted(session)
    track = audio.ActiveTrack()
    for _ in range(20):
        track.looked()
        track.codec(session)
    assert asked == [0]


def test_a_read_that_failed_is_tried_again_at_the_next_ask():
    """Not kept as "no codec" until something else happens."""
    session = session_with([AudioTrackInfo("MPEG"), AudioTrackInfo("AC3")])
    tracks = session.nav.service._audio
    real = tracks.getTrackInfo
    calls = []

    def flaky(index):
        calls.append(index)
        if len(calls) == 1:
            raise RuntimeError("no program information yet")
        return real(index)

    tracks.getTrackInfo = flaky
    tracks.current = 1
    track = audio.ActiveTrack()
    assert track.codec(session) is None
    assert track.codec(session) == "ac3"
    assert track.codec(session) == "ac3"
    assert calls == [1, 1]


def test_a_track_that_describes_itself_with_nothing_is_not_a_failed_read():
    session = session_with([AudioTrackInfo("")])
    asked = counted(session)
    track = audio.ActiveTrack()
    assert [track.codec(session) for _ in range(4)] == [None] * 4
    assert asked == [0]


def test_an_active_track_that_cannot_be_named_or_described_has_no_codec():
    track = audio.ActiveTrack()
    assert track.codec(None) is None
    assert track.codec(session_with([])) is None
    session = session_with([AudioTrackInfo("AC3")])
    assert track.codec(session) == "ac3"
    session.nav.service._audio.raises = True
    assert track.codec(session) is None
    session.nav.service._audio.raises = False
    session.nav.service._audio.current = 9
    assert track.codec(session) is None
    session.nav.service._audio.current = 0
    assert track.codec(session) == "ac3"


# ---- AAC and the separate transcode switch (`config.av.transcodeaac`)


def transcode(value):
    return ConfigSelection(choices=["off", "ac3", "dts", "force_ac3"], default=value)


@pytest.mark.parametrize("value", ["ac3", "dts", "force_ac3"])
@pytest.mark.parametrize("setting", ["downmix", "passthrough"])
def test_aac_that_is_being_transcoded_cannot_be_told(av, value, setting):
    """A transcode is neither of the two certain answers, whatever the downmix setting says."""
    av.downmix_aac = word(setting)
    av.transcodeaac = transcode(value)
    assert audio.passthrough_of(active("AAC")) is None
    assert audio.passthrough_of(active("AAC-LC")) is None


def test_he_aac_that_is_being_transcoded_cannot_be_told(av):
    av.downmix_aacplus = word("downmix")
    av.transcodeaac = transcode("ac3")
    assert audio.passthrough_of(active("HE-AAC")) is None


@pytest.mark.parametrize("value", ["off", " Off "])
def test_a_transcode_that_is_off_leaves_the_downmix_setting_to_decide(av, value):
    av.downmix_aac = word("downmix")
    av.downmix_aacplus = word("passthrough")
    av.transcodeaac = type("Element", (), {"value": value})()
    assert audio.passthrough_of(active("AAC")) is False
    assert audio.passthrough_of(active("HE-AAC")) is True


def test_the_placeholder_where_a_receiver_cannot_transcode_is_no_transcode(av):
    av.downmix_aac = word("passthrough")
    av.transcodeaac = type("Element", (), {"value": ""})()
    assert audio.passthrough_of(active("AAC")) is True


@pytest.mark.parametrize("value", [True, False])
def test_a_transcode_switch_that_is_a_yes_or_no_is_not_the_one_the_images_have(av, value):
    av.downmix_aac = word("passthrough")
    av.transcodeaac = type("Element", (), {"value": value})()
    assert audio.passthrough_of(active("AAC")) is True


def test_the_aac_transcode_is_no_business_of_another_codec(av):
    av.downmix_ac3 = word("passthrough")
    av.downmix_dts = word("downmix")
    av.transcodeaac = transcode("ac3")
    assert audio.passthrough_of(active("AC3")) is True
    assert audio.passthrough_of(active("DTS")) is False
    assert audio.passthrough_of(active("MPEG")) is False


def test_whether_the_image_has_any_of_the_settings(av):
    assert audio.settings_known() is False
    av.unrelated = word("passthrough")
    assert audio.settings_known() is False
    av.dtshd = word("downmix")
    assert audio.settings_known() is True


def test_an_image_without_an_av_section_has_none_of_the_settings():
    assert audio.settings_known() is False


def test_every_setting_named_is_for_a_codec_the_table_can_produce():
    assert set(audio.SETTINGS) <= set(audio.CODECS.values())
    assert set(audio.ALWAYS_DECODED) <= set(audio.CODECS.values())
    assert not set(audio.ALWAYS_DECODED) & set(audio.SETTINGS)
