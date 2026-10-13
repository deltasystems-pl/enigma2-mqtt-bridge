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
plugin cannot see, and is `None`.

Nothing here reads a file. The settings are attributes of objects the image
already holds in memory, and the tracks are calls into the playing service.
"""

from .enigma2 import current_service
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

    One call into the service, for the look that only asks whether the track
    changed.
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


def active_of(tracks):
    """The index of the active track in a list `read_tracks` built, or None."""
    for track in tracks or ():
        if track.get("active"):
            return track.get("index")
    return None


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


def passthrough_of(tracks):
    """Whether the active track of `tracks` is passed to the television undecoded.

    True, False, or None when it cannot be told: no active track, a codec this
    plugin does not know, an image without the setting for it, or a setting
    whose value is neither of the two this plugin is sure of.
    """
    for track in tracks or ():
        if not track.get("active"):
            continue
        codec = track.get("codec")
        if codec in ALWAYS_DECODED:
            return False
        for name in SETTINGS.get(codec, ()):
            value = _setting(name)
            if value is not None:
                return _decides(value)
        return None
    return None


def passthrough(session):
    """`passthrough_of` for what is playing now."""
    return passthrough_of(read_tracks(session))


def settings_known():
    """Whether this image has at least one of the settings passthrough is read from."""
    return any(_setting(name) is not None for names in SETTINGS.values() for name in names)
