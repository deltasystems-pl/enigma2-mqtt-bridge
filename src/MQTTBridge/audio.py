"""The audio tracks of the playing service, and whether the receiver's volume acts on them.

Two publishers read this: `service` lists the tracks, and `volume` says whether
the active one is handed to the television undecoded - in which case the
receiver's own volume changes nothing anybody can hear.

**A track's codec is read off its description**, because that is all the image
offers: `iAudioTrackInfo` has a description, a language and a PID, and on the
measured image no type. The description is a word the image chose and the three
images chose differently - "AC3", "Dolby Digital" - so the table below holds
every spelling their `getTrackInfo` writes for a DVB service, and nothing else.
A description that is not in it is an unknown codec, `None`, never a guess: a
stream played through the media player describes its audio in other words, and
those are not mapped.

**Passthrough is the image's setting for the active track's codec**
(ADR-0019). The settings live in `config.av`, are named differently per codec
and per image, and are a word on some images and a boolean on others. Only the
two answers that are certain are taken - "downmix" and "passthrough", or the
boolean that stands for them. Every other choice ("use the best the television
offers", "convert to multi-channel PCM", a transcode) depends on something this
plugin cannot see, and is `None`. AAC has a transcode switch of its own beside
its downmix setting (`transcodeaac`); with that on, the answer is `None` too.

**Passthrough needs one track, not all of them.** `ActiveTrack` asks which
track is playing and reads that one's description, once, until the track or
the service is another: the volume is read on every key press, and on one
image describing a track starts work in the decoder. Once is not for ever: a
track can change under the same index with nothing said, so the description
is read again after a number of periodic looks, and a read that failed is
tried again at the next periodic look - not at the next ask, which may be a
key press.

Nothing here reads a file. The settings are attributes of objects the image
already holds in memory, and the tracks are calls into the playing service.
"""

from .enigma2 import current_service, current_service_reference
from .log import get_logger

LOG = get_logger("audio")

# A service carries a handful of tracks. The bound is for the one that does not.
MAX_TRACKS = 32

# The image's words and a language tag are short; these keep a broken service
# from putting a page of text on a retained topic.
DESCRIPTION_LIMIT = 64
LANGUAGE_LIMIT = 64

# What each image's `eDVBServicePlay::getTrackInfo` writes, lower-cased:
# OpenATV and OpenPLi ("AC3", "AC3+", "AAC", "AAC-HE", "AC4"), OpenViX ("Dolby
# Digital", "Dolby Digital +", "AAC-LC", "HE-AAC", "Dolby AC-4", and "Dolby
# Atmos" for an E-AC-3 track that carries it), and the ones all three share.
CODECS = {
    "mpeg": "mp2",
    "ac3": "ac3",
    "dolby digital": "ac3",
    "ac3+": "eac3",
    "dolby digital +": "eac3",
    "dolby atmos": "eac3",
    "aac": "aac",
    "aac-lc": "aac",
    "aac-he": "aac_he",
    "he-aac": "aac_he",
    "dts": "dts",
    "dts-hd": "dts_hd",
    "lpcm": "lpcm",
    "dra": "dra",
    "ac4": "ac4",
    "dolby ac-4": "ac4",
}

# Decoded by the receiver whatever is set: no image offers to pass these on.
ALWAYS_DECODED = ("mp2", "lpcm")

# codec -> the `config.av` elements that decide it, the first one the image has.
# E-AC-3 is `downmix_ac3plus` on OpenViX and `transcodeac3plus` on OpenATV and
# OpenPLi. HE-AAC has a setting of its own only on some receivers, and where it
# has none nothing here says which one governs it.
SETTINGS = {
    "ac3": ("downmix_ac3",),
    "eac3": ("downmix_ac3plus", "transcodeac3plus"),
    "aac": ("downmix_aac",),
    "aac_he": ("downmix_aacplus",),
    "dts": ("downmix_dts",),
    "dts_hd": ("dtshd",),
}

# codec -> the `config.av` element that turns a transcode of it on, beside its
# downmix setting: "off", "ac3" or "dts" on all three images ("force_ac3" on
# two OpenATV models). Anything but "off" is a transcode, and what reaches the
# television then is not what the downmix setting says.
TRANSCODES = {
    "aac": "transcodeaac",
    "aac_he": "transcodeaac",
}
TRANSCODE_OFF = "off"


def codec_name(description):
    """A stable name for the codec an image's track description stands for, or None."""
    if not isinstance(description, str):
        return None
    return CODECS.get(description.strip().lower())


def _selection(session):
    """The playing service's audio track selection, or None."""
    service = current_service(session)
    getter = getattr(service, "audioTracks", None) if service is not None else None
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        LOG.debug("the playing service has no audio track selection")
        return None


def _count(selection):
    try:
        count = int(selection.getNumberOfTracks())
    except Exception:
        return None
    return count if count >= 0 else None


def active_index(session):
    """The index of the track being played, or None when the image will not say.

    Two calls into the service - how many tracks, and which one - and no
    track is read: this is the look that only asks whether the track changed.
    """
    selection = _selection(session)
    if selection is None or not _count(selection):
        return None
    try:
        index = int(selection.getCurrentTrack())
    except Exception:
        return None
    return index if index >= 0 else None


def _text(info, method, limit):
    getter = getattr(info, method, None)
    if getter is None:
        return None
    try:
        value = getter()
    except Exception:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] or None


def _pid(info):
    getter = getattr(info, "getPID", None)
    if getter is None:
        return None
    try:
        pid = int(getter())
    except Exception:
        return None
    return pid if pid >= 0 else None


def read_tracks(session):
    """The tracks of the playing service as the `service` topic lists them, or None.

    None when nothing is playing or the image will not say; `[]` for a service
    that has no audio. At most `MAX_TRACKS`, in the image's order. A track the
    image cannot describe is still listed, by its index, with nulls.
    """
    selection = _selection(session)
    if selection is None:
        return None
    count = _count(selection)
    if count is None:
        return None
    try:
        current = int(selection.getCurrentTrack()) if count else -1
    except Exception:
        current = -1
    tracks = []
    for index in range(min(count, MAX_TRACKS)):
        try:
            info = selection.getTrackInfo(index)
        except Exception:
            info = None
        description = _text(info, "getDescription", DESCRIPTION_LIMIT)
        tracks.append({
            "index": index,
            "description": description,
            "language": _text(info, "getLanguage", LANGUAGE_LIMIT),
            "pid": _pid(info),
            "codec": codec_name(description),
            "active": index == current,
        })
    return tracks


class ActiveTrack:
    """The codec of the track being played, read once for each track of each service.

    The look costs the index of the playing track and the playing reference;
    the track's description is read only when one of the two is not what it
    was at the last look. `forget()` is for a service event, after which the
    same index of the same reference may be another track.

    Two things keep an answer from outliving the track it was read from.
    `looked()` is called once for each periodic look, and after
    `reread_looks` of them the description is read again - so the bound is
    that many looks, and asking in between (a volume key press) neither
    reads nor brings the next read nearer. And a read that failed is kept
    only until the next look: that look forgets it, so the ask after it
    reads again, while every ask before it is answered "unknown" unread.
    """

    def __init__(self, reread_looks=None):
        # How many periodic looks an answer is good for; None for no bound.
        self.reread_looks = reread_looks
        self._looks = 0
        self._key = None
        self._codec = None
        self._failed = False

    def forget(self):
        self._key = None
        self._failed = False

    def looked(self):
        """One periodic look has gone by."""
        if self._failed:
            self.forget()
        if not self.reread_looks:
            return
        self._looks += 1
        if self._looks >= self.reread_looks:
            self.forget()

    def codec(self, session):
        index = active_index(session)
        if index is None:
            self._key = None
            return None
        key = (current_service_reference(session), index)
        if key != self._key:
            self._looks = 0
            try:
                info = _selection(session).getTrackInfo(index)
            except Exception:
                # Not an answer, and kept as "unknown" for this track until
                # the next periodic look, which is when it is tried again.
                self._codec = None
                self._failed = True
            else:
                self._codec = codec_name(_text(info, "getDescription", DESCRIPTION_LIMIT))
                self._failed = False
            self._key = key
        return self._codec


# ---------------------------------------------------------------- passthrough --


def _setting(name):
    """`config.av.<name>` as the image holds it: a word, a boolean, or None.

    None where the image has no such element, and where the element is the
    placeholder an image puts there when the receiver cannot do it - whose
    value is the empty string.
    """
    try:
        from Components.config import config
    except Exception:
        return None
    section = getattr(config, "av", None)
    element = getattr(section, name, None) if section is not None else None
    if element is None:
        return None
    try:
        value = element.value
    except Exception:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None


def _decides(value):
    """Whether a setting's value means passthrough: True, False, or None for neither.

    A word on OpenViX and OpenPLi and on some OpenATV receivers; a boolean on
    the others, where true is "downmix".
    """
    if value is True:
        return False
    if value is False:
        return True
    if value == "passthrough":
        return True
    if value == "downmix":
        return False
    return None


def passthrough_for(codec):
    """Whether a track of `codec` is passed to the television undecoded.

    True, False, or None when it cannot be told: a codec this plugin does not
    know, an image without the setting for it, a setting whose value is
    neither of the two this plugin is sure of, or a transcode of it switched
    on.
    """
    if codec in ALWAYS_DECODED:
        return False
    transcode = _setting(TRANSCODES.get(codec, ""))
    if isinstance(transcode, str) and transcode != TRANSCODE_OFF:
        # A word on every image that has the switch; a yes/no there is no
        # switch this plugin knows, and is left to the downmix setting.
        return None
    for name in SETTINGS.get(codec, ()):
        value = _setting(name)
        if value is not None:
            return _decides(value)
    return None


def passthrough_of(tracks):
    """`passthrough_for` the active track of a list `read_tracks` built; None without one."""
    for track in tracks or ():
        if track.get("active"):
            return passthrough_for(track.get("codec"))
    return None


def settings_known():
    """Whether this image has at least one of the settings passthrough is read from."""
    return any(_setting(name) is not None for names in SETTINGS.values() for name in names)
