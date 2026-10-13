"""The audio tracks of the playing service, as the `service` topic lists them.

**A track's codec is read off its description**, because that is all the image
offers: `iAudioTrackInfo` has a description, a language and a PID, and on the
measured image no type. The description is a word the image chose and the three
images chose differently - "AC3", "Dolby Digital" - so the table below holds
every spelling their `getTrackInfo` writes for a DVB service, and nothing else.
A description that is not in it is an unknown codec, `None`, never a guess: a
stream played through the media player describes its audio in other words, and
those are not mapped.

Nothing here reads a file: the tracks are calls into the playing service.
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
