"""The recording library: every recording in the receiver's movie locations.

**Read on events, never on a timer.** A recording disk spins down when nobody uses
it, and a library read is the one thing in this plugin that reads directories and
the files beside each recording. So it is read once when the plugin starts, and
then only when something changed it: a recording started or ended, a delete this
plugin made, a movie location that was mounted - and `cmd/recordings_refresh`,
which is the only request that may wake a disk on purpose. Free space comes from
`statvfs`, which a disk answers from its superblock without spinning up, and whether
a location is mounted comes from `/proc/mounts`, which touches no disk at all; both
are read on the recording disk's existing minute timer (`hdd.py`), in its thread.

**Files are read off the main loop.** The walk, the `.meta` files, the `.ap` files
and the encoding of each folder's payload run in a worker thread; the image's own
API - a channel's name, the channel list's bouquets - is asked on the main loop
only, in slices, so a library of thousands of recordings never holds the picture
up. One read runs at a time; a reason that arrives during a read is read again
after it, and a refresh during a read is answered by that read.

**A recording is addressed by an id, never by a path.** The id is the first 16
hex digits of the sha256 of its full path's bytes. Commands take the id, and the
plugin acts only on a recording its own current library lists - so nothing that
arrives from the broker ever names a file.

**A delete only ever moves into the image's trash** (`delete`): the image's own
move, with the files it moves beside each recording, and refused outright when
the image's trash is switched off. The image's own delete-for-good path is never
taken.

The receiver lists everything. Which recordings Home Assistant shows - its hidden
bouquets - is Home Assistant's to decide, from `sref` and `bouquet` on each entry.
"""

import hashlib
import importlib
import json
import os
import stat
import threading
import time

from .channels import slugify, topic_slugs
from .enigma2 import (
    Ticker,
    enigma_attribute,
    missing,
    reference_string,
    service_name,
)
from .log import get_logger
from .origin import MQTT, granted
from .publisher import Publisher, Refusal

LOG = get_logger("library")

INDEX = "recordings"
FOLDER_PREFIX = "recordings/"

CAPABILITY = "recordings"
PLAY_CAPABILITY = "recording_play"
DELETE_CAPABILITY = "recording_delete"
PERMISSION = "recording_delete_allowed"

# Why the library was read, as `recordings.reason` says it.
START = "start"
RECORDING = "recording"
DELETE = "delete"
MOUNT = "mount"
REFRESH = "refresh"

# What a recording is. The image's movie list shows other media files too; they are
# not recordings, and the library leaves them out.
RECORDING_EXTENSIONS = (".ts", ".stream")

# The files the image's own move takes along with a recording (`createMoveList`), which
# the library also reads from: the `.meta` and the `.ap`.
META_SUFFIX = ".meta"
AP_SUFFIX = ".ap"

ID_LENGTH = 16
ID_CHARACTERS = frozenset("0123456789abcdef")

# Text read from a recording's own files is cut here: a garbled `.meta` can carry a line
# of four thousand characters.
TEXT_LIMIT = 200

BYTES_PER_MEGABYTE = 1024 * 1024
META_READ_LIMIT = 16384

# The 90 kHz clock of an MPEG transport stream, and its 33-bit wrap.
PTS_PER_SECOND = 90000
PTS_WRAP = 1 << 33
# A duration past this is not one: a garbled `.ap` file.
LONGEST_SECONDS = 7 * 24 * 3600

# How long a burst of record events is given to end before the library is read.
RECORD_SETTLE_MILLISECONDS = 3000

# The main loop's share of a read: work is done in slices that end once this much time
# has gone, and the next slice runs on the next turn.
SLICE_SECONDS = 0.03
NAMES_PER_SLICE = 200

# How long a read may take before it is given up - a movie location on a share that stopped
# answering holds its worker for as long as the share does.
READ_WATCHDOG_SECONDS = 600

# How long a delete waits for the image's move to have taken the recording away. The
# image moves a large file in a background job, behind any job queued before it.
DELETE_POLL_MILLISECONDS = 500
DELETE_WAIT_SECONDS = 60

MOUNTS_PATH = "/proc/mounts"

# Refusals. A pair carries its reason code on `last_error`; a plain sentence has none. The
# codes are the contract's: `not_in_library`, `file_gone`, `not_permitted`, `trash_off`,
# `in_use`, `in_progress` and `timeshift_kept` - and `busy`, which only the doors' sentence
# carries (`commands.py`).
NOT_AVAILABLE = "the recording library is not available on this image"
NO_ID = "the command takes the id of one recording: {\"id\": \"<16 hex digits>\"}"
UNKNOWN = ("that recording is not in the receiver's library", "not_in_library")
GONE = ("that recording's file is gone from the receiver", "file_gone")
NOT_PERMITTED = (
    "deleting recordings is switched off in the plugin's settings", "not_permitted")
TRASH_OFF = (
    "the receiver's trash is switched off; nothing is deleted for good", "trash_off")
NO_TRASH = "this image has no trash for recordings; nothing is deleted for good"
RECORDED = ("that recording is being recorded", "in_use")
PLAYED = ("that recording is being played", "in_use")
MOVE_FAILED = "the receiver could not move that recording to its trash"
NOT_MOVED = "the receiver did not move that recording to its trash"
NO_PLAYER = "this image's movie player cannot be opened"
IN_PROGRESS = ("another recording is being moved to the trash; try again", "in_progress")
TIMESHIFT_KEPT = (
    "the viewer kept timeshift on the receiver; the recording was not played", "timeshift_kept")


def refusal(pair):
    """A refusal as a handler returns it: with its reason code when it has one."""
    if isinstance(pair, str):
        return pair
    sentence, reason = pair
    return Refusal(sentence, reason)


# ------------------------------------------------------------------- the id --


def recording_id(path):
    """The id of the recording at `path`: 16 hex digits of the sha256 of the path's bytes."""
    return hashlib.sha256(os.fsencode(path)).hexdigest()[:ID_LENGTH]


def shown(text):
    """A path or a name as UTF-8 can carry it: bytes it cannot are replacement characters."""
    if isinstance(text, bytes):
        return text.decode("utf-8", "replace")
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return os.fsencode(text).decode("utf-8", "replace")


def valid_id(value):
    return (
        isinstance(value, str)
        and len(value) == ID_LENGTH
        and ID_CHARACTERS.issuperset(value)
    )


def _cut(text):
    text = " ".join(str(text).split()) if text is not None else ""
    return text[:TEXT_LIMIT] if text else None


# --------------------------------------------------------- the image's side --


def _config():
    try:
        from Components.config import config

        return config
    except Exception:
        return None


def _element(section, name):
    config = _config()
    owner = getattr(config, section, None) if config is not None else None
    return getattr(owner, name, None) if owner is not None else None


def _videodirs():
    """`config.movielist.videodirs`, which the image's movie list defines when it is imported."""
    element = _element("movielist", "videodirs")
    if element is None:
        try:
            # Imported for what importing it does: it defines config.movielist.videodirs.
            importlib.import_module("Screens.MovieSelection")
        except Exception as error:
            missing("Screens.MovieSelection", error)
            return None
        element = _element("movielist", "videodirs")
    if element is None:
        missing("config.movielist.videodirs")
    return element


def configured_locations():
    """`[(location, extra), ...]` as the receiver is configured now, or None without a movie list.

    The default movie location first (`config.usage.default_path`), then the movie list's
    own locations in the image's order, then the timer and the instant-record locations
    where they name a folder of their own - `extra`, for they are listed only where no
    location before them holds them already. Spelled the way the settings hold them,
    normalised and without repeats - nothing here touches a disk.
    """
    videodirs = _videodirs()
    if videodirs is None:
        return None
    candidates = []

    def setting(name):
        element = _element("usage", name)
        try:
            return str(element.value or "") if element is not None else ""
        except Exception:
            return ""

    candidates.append((setting("default_path"), False))
    try:
        candidates.extend((value, False) for value in list(videodirs.value or []))
    except Exception:
        LOG.exception("config.movielist.videodirs could not be read")
        return None
    for name in ("timer_path", "instantrec_path"):
        candidates.append((setting(name), True))
    locations = []
    seen = set()
    for candidate, extra in candidates:
        if not isinstance(candidate, str) or not candidate.startswith("/"):
            continue
        location = os.path.normpath(candidate)
        if location not in seen:
            seen.add(location)
            locations.append((location, extra))
    return locations


def movie_locations():
    """The configured movie locations in their order (`configured_locations`), or None."""
    configured = configured_locations()
    return None if configured is None else [location for location, _extra in configured]


def _trash_module():
    try:
        import Tools.Trashcan as trashcan
    except Exception:
        return None
    return trashcan


def _create_trash():
    """The image's function that finds - and makes - the trash folder for a path, or None.

    OpenViX and OpenPLi call it `createTrashFolder`; OpenATV `createTrashcan`, with the
    first name as an alias of it.
    """
    module = _trash_module()
    if module is None:
        return None
    for name in ("createTrashFolder", "createTrashcan"):
        found = getattr(module, name, None)
        if callable(found):
            return found
    return None


def trash_state():
    """`info.recording_trash`: whether the image's trash is on, or None without a trash.

    `config.usage.movielist_trashcan` is the image's own switch, on by default on
    OpenViX, OpenATV and OpenPLi alike.
    """
    if _create_trash() is None:
        return None
    element = _element("usage", "movielist_trashcan")
    if element is None:
        return None
    try:
        return bool(element.value)
    except Exception:
        return None


def _move_service_files():
    try:
        from Screens.MovieSelection import moveServiceFiles
    except Exception as error:
        missing("Screens.MovieSelection.moveServiceFiles", error)
        return None
    return moveServiceFiles


def _forget_resume_point(reference):
    """The image's own `delResumePoint`, as its movie list calls it after a move."""
    try:
        from Screens import InfoBarGenerics
    except Exception:
        return
    instance = getattr(InfoBarGenerics, "resumePointsInstance", None)
    forget = getattr(instance, "delResumePoint", None) if instance is not None else None
    if forget is None:
        forget = getattr(InfoBarGenerics, "delResumePoint", None)
    if forget is None:
        return
    try:
        forget(reference)
    except Exception:
        LOG.debug("the image's resume point could not be removed")


def _strtoul16(pair):
    """`strtoul(pair, 0, 16)` of two characters, as the image's `urlDecode` calls it, as a byte.

    Leading white space is skipped, a sign is taken - a minus negates in unsigned
    arithmetic - then a `0x` prefix where a hex digit follows it, then hex digits up to
    the first that is none.
    """
    index = 0
    while index < len(pair) and pair[index] in " \t\n\v\f\r":
        index += 1
    negative = False
    if index < len(pair) and pair[index] in "+-":
        negative = pair[index] == "-"
        index += 1
    digits = "0123456789abcdefABCDEF"
    if (pair[index:index + 2] in ("0x", "0X") and index + 2 < len(pair)
            and pair[index + 2] in digits):
        index += 2
    value = 0
    while index < len(pair) and pair[index] in digits:
        value = value * 16 + int(pair[index], 16)
        index += 1
    if negative:
        value = -value
    return value & 0xFF


def decode_path(text):
    """The image's `urlDecode`: `%` and the two characters after it are one byte.

    The two are read by `strtoul` (`_strtoul16`), and a byte of zero is dropped, as the
    image drops it.
    """
    raw = text.encode("utf-8", "surrogateescape")
    out = bytearray()
    index = 0
    while index < len(raw):
        byte = raw[index]
        if byte != 0x25:
            out.append(byte)
        else:
            index += 2
            if index >= len(raw):
                break
            value = _strtoul16(raw[index - 1:index + 1].decode("latin-1"))
            if value:
                out.append(value)
        index += 1
    return out.decode("utf-8", "surrogateescape")


def file_of(reference):
    """The file a reference plays - its path, decoded as the image decodes it - or "".

    Asked of the reference itself where it can say (`getPath()`); a reference in its
    string form is read as the image reads one: the ten numbers, then the path up to the
    next colon, then its `%` escapes.
    """
    getter = getattr(reference, "getPath", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:
            return ""
    parts = reference_string(reference).split(":", 10)
    if len(parts) <= 10:
        return ""
    return decode_path(parts[10].split(":", 1)[0])


def playable_reference(path):
    """The reference the image's movie list plays a recording file with.

    Built with the image's own constructor - kind, flags and the path - so the path is the
    file's whatever it holds: a `:`, a `%` or a space. A string put together by hand would
    be read up to its first colon, and with its `%` escapes undone.
    """
    factory = enigma_attribute("eServiceReference")
    if factory is None:
        return None
    kind = 4097 if path.lower().endswith(".stream") else 1
    try:
        return factory(kind, 0, path)
    except Exception:
        LOG.exception("could not build a reference for a recording")
        return None


def _infobar():
    try:
        from Screens.InfoBar import InfoBar
    except Exception:
        return None
    return getattr(InfoBar, "instance", None)


def _movie_player_class():
    try:
        from Screens.InfoBar import MoviePlayer

        return MoviePlayer
    except Exception:
        return None


def player_available():
    """Whether a recording can be handed to the image's movie player: there is an info bar."""
    infobar = _infobar()
    if infobar is None or _movie_player_class() is None:
        return False
    return callable(getattr(infobar, "openMoviePlayer", None)) or callable(
        getattr(infobar, "movieSelected", None)
    )


# ------------------------------------------------------------- the mounts --


def _unescape_mount(text):
    """`/proc/mounts` writes a space as `\\040`, a tab as `\\011`, a backslash as `\\134`."""
    out = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 4 <= len(text) and text[index + 1:index + 4].isdigit():
            try:
                out.append(chr(int(text[index + 1:index + 4], 8)))
                index += 4
                continue
            except ValueError:
                pass
        out.append(text[index])
        index += 1
    return "".join(out)


def read_mounts(path=MOUNTS_PATH):
    """`[(mount point, file system type), ...]` in the kernel's order, or [] when unreadable."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    mounts = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mounts.append((_unescape_mount(parts[1]), parts[2]))
    return mounts


def _under(path, point):
    if point == "/":
        return path.startswith("/")
    return path == point or path.startswith(point + "/")


def covering_mount(path, mounts):
    """`(mount point, type)` of the mount `path` is on: the longest point, the last one listed."""
    found = ("/", "")
    for point, kind in mounts:
        if _under(path, point) and len(point) >= len(found[0]):
            found = (point, kind)
    return found


def is_mounted(path, mounts):
    """Whether `path` is on a mounted file system of its own: not the root, not autofs waiting.

    An automounter's own entry is what `/proc/mounts` lists for a share that is not
    mounted yet, and anything that walked into it would mount it.
    """
    point, kind = covering_mount(path, mounts)
    return point != "/" and kind != "autofs"


def resolve(path, mounts, depth=0):
    """`path` with the symbolic links resolved that lie on the root file system.

    `/hdd/movie` is `/media/hdd/movie` on most images. Only the links on the flash are
    followed, never one inside a mounted file system: reading that would be a disk
    access, and on an automounted share a mount.
    """
    if depth > 8:
        return path
    current = "/"
    parts = [part for part in path.split("/") if part]
    for index, part in enumerate(parts):
        candidate = os.path.join(current, part)
        if covering_mount(candidate, mounts)[0] != "/":
            return os.path.normpath(os.path.join(candidate, *parts[index + 1:]))
        try:
            linked = os.path.islink(candidate)
        except OSError:
            linked = False
        if linked:
            try:
                target = os.readlink(candidate)
            except OSError:
                return os.path.normpath(os.path.join(candidate, *parts[index + 1:]))
            target = os.path.normpath(os.path.join(current, target))
            rest = os.path.join(target, *parts[index + 1:]) if parts[index + 1:] else target
            return resolve(rest, mounts, depth + 1)
        current = candidate
    return os.path.normpath(current)


def free_megabytes(path):
    try:
        status = os.statvfs(path)
        return int(status.f_bavail * status.f_frsize / BYTES_PER_MEGABYTE)
    except (OSError, AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None


def space(locations, mounts_path=MOUNTS_PATH):
    """{location: (mounted, free_mb)} - `/proc/mounts`, and `statvfs` only where mounted.

    For the recording disk's thread (`hdd.py`): a `statvfs` on a network share that
    stopped answering can take as long as the share, and never on the main loop.
    """
    mounts = read_mounts(mounts_path)
    result = {}
    for location in locations or ():
        real = resolve(location, mounts)
        mounted = is_mounted(real, mounts)
        result[location] = (mounted, free_megabytes(real) if mounted else None)
    return result


def kept_locations(locations, extras, mounts):
    """`[(location, resolved), ...]` of the locations a read lists, in their order.

    A location whose resolved path repeats an earlier one is left out, and so is one of
    `extras` - the timer and instant-record locations - that an earlier location holds.
    Resolving reads only links on the flash (`resolve`), never a disk.
    """
    kept = []
    for location in locations:
        real = resolve(location, mounts)
        if any(real == other for _, other in kept):
            continue
        if location in extras and any(_under(real, other) for _, other in kept):
            continue
        kept.append((location, real))
    return kept


# --------------------------------------------------------- one recording --


def parse_meta(data):
    """What a `.meta` file says, as far as it says it: a dict of the lines the library uses.

    The image writes, one per line: the service reference, the name, the short
    description, the creation time, the tags, the length in 90 kHz ticks, the size,
    and more after that. A line that is missing or does not parse is left out.
    """
    if not data:
        return {}
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    lines = [line.rstrip("\r") for line in text.split("\n")]
    meta = {}

    def line(index):
        return lines[index] if index < len(lines) else ""

    sref = line(0).strip()
    if sref and sref.count(":") >= 9 and "\x00" not in sref:
        meta["sref"] = sref[:TEXT_LIMIT * 2]
    if line(1).strip():
        meta["name"] = _cut(line(1))
    if line(2).strip():
        meta["description"] = _cut(line(2))
    for index, key in ((3, "created"), (5, "length")):
        value = line(index).strip()
        if value.isdigit():
            number = int(value)
            if number > 0:
                meta[key] = number
    return meta


def ap_duration(path):
    """The length of a recording from its `.ap` file - the first and the last access point.

    Each entry is sixteen bytes: an offset and a presentation time, both 64-bit
    big-endian. Two reads of sixteen bytes, where the image's own reading opens
    the recording itself when its `.meta` has no length.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size < 32 or size % 16:
                return None
            handle.seek(0)
            first = handle.read(16)
            handle.seek(size - 16)
            last = handle.read(16)
    except OSError:
        return None
    return _points_duration(first, last)


def ap_duration_bytes(data):
    """`ap_duration` of an `.ap` file's whole content."""
    if len(data) < 32 or len(data) % 16:
        return None
    return _points_duration(data[:16], data[-16:])


def _points_duration(first, last):
    if len(first) != 16 or len(last) != 16:
        return None
    begin = int.from_bytes(first[8:], "big") % PTS_WRAP
    end = int.from_bytes(last[8:], "big") % PTS_WRAP
    seconds = ((end - begin) % PTS_WRAP) // PTS_PER_SECOND
    if seconds <= 0 or seconds > LONGEST_SECONDS:
        return None
    return int(seconds)


def split_name(stem):
    """`(channel, title)` from the image's own file name, `YYYYMMDD HHMM - Channel - Title`.

    What the library falls back to where a recording has no `.meta` to say it. Either
    is None when the name does not have that shape.
    """
    parts = stem.split(" - ", 2)
    head = parts[0]
    stamp = head.replace(" ", "")
    if len(parts) >= 2 and stamp.isdigit() and len(stamp) in (8, 12):
        if len(parts) == 3:
            return _cut(parts[1]), _cut(parts[2])
        return None, _cut(parts[1])
    return None, None


def read_recording(folder, name, location, stats=None):
    """One recording as the library lists it, or None when the file cannot be read.

    The worker's half: files only. The channel's name from the image and the bouquet
    are added on the main loop.
    """
    path = os.path.join(folder, name)
    try:
        status = stats if stats is not None else os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode):
        return None
    meta = {}
    try:
        with open(path + META_SUFFIX, "rb") as handle:
            meta = parse_meta(handle.read(META_READ_LIMIT))
    except OSError:
        meta = {}
    except Exception:
        LOG.debug("a .meta file could not be parsed")
        meta = {}
    stem = name[: name.rfind(".")] if "." in name else name
    channel, title = split_name(shown(stem))
    duration = None
    if meta.get("length"):
        seconds = meta["length"] // PTS_PER_SECOND
        if 0 < seconds <= LONGEST_SECONDS:
            duration = int(seconds)
    if duration is None:
        duration = ap_duration(path + AP_SUFFIX)
    begin = meta.get("created") or int(getattr(status, "st_ctime", 0) or 0) or None
    return {
        "id": recording_id(path),
        "file": shown(os.path.relpath(path, location)),
        "name": meta.get("name") or title or _cut(shown(stem)),
        "description": meta.get("description"),
        "sref": meta.get("sref"),
        "begin": begin,
        "duration": duration,
        "size_mb": int(status.st_size // BYTES_PER_MEGABYTE),
        "_path": path,
        "_channel": channel,
    }


def _is_recording(name):
    lowered = name.lower()
    return any(lowered.endswith(extension) for extension in RECORDING_EXTENSIONS)


def walk(location, skip=()):
    """`[(folder relative to the location, [recording, ...]), ...]` of one movie location.

    Every folder under it, the trash and every other hidden folder left out, and a
    folder that is a link left out as well - followed, a link back up the tree would
    walk for ever, and a link to another location would list its recordings twice.
    Left out too: a folder in `skip` - another movie location, walked by itself - and
    a folder on another file system, a disk or a share mounted inside this one.
    Folders without a recording are not listed.
    """
    folders = []
    pending = [location]
    seen = set()
    skip = set(skip)
    try:
        device = os.stat(location).st_dev
    except OSError:
        device = None
    while pending:
        folder = pending.pop(0)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        try:
            key = os.stat(folder)
            key = (key.st_dev, key.st_ino)
        except OSError:
            key = None
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        recordings = []
        subfolders = []
        for entry in sorted(entries, key=lambda item: item.name):
            name = entry.name
            if name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.path in skip:
                        continue
                    if device is not None and entry.stat(follow_symlinks=False).st_dev != device:
                        continue
                    subfolders.append(entry.path)
                    continue
                if entry.is_symlink() or not _is_recording(name):
                    continue
                stats = entry.stat()
            except OSError:
                continue
            record = read_recording(folder, name, location, stats)
            if record is not None:
                recordings.append(record)
        relative = os.path.relpath(folder, location)
        if recordings:
            folders.append(("" if relative == "." else shown(relative), recordings))
        pending.extend(subfolders)
    return folders


# ------------------------------------------------------- the published shape --


ENTRY_MEMBERS = (
    "id", "file", "name", "description", "service_name", "sref", "bouquet",
    "begin", "duration", "size_mb",
)


def newest_first(recordings):
    return sorted(recordings, key=lambda record: (-(record.get("begin") or 0), record["file"]))


def folder_slugs(folders):
    """A slug for each `(location, relative path)` - unique in the node, in a fixed order.

    A name that leaves no slug - Cyrillic, Greek, Arabic - is `folder` before the slugs
    are made unique, so three such folders are `folder`, `folder_2` and `folder_3` and
    never one topic.
    """
    names = []
    for location, relative in folders:
        base = os.path.basename(location.rstrip("/")) or "recordings"
        names.append(slugify(base + ("_" + relative if relative else "")) or "folder")
    return topic_slugs(names)


def fit(entries, encode, allowed):
    """`(encoded, cut)`: a folder's payload, newest first, its oldest left out until it fits.

    `allowed` is how many bytes of payload this folder's topic leaves under the packet
    bound. Each entry is encoded once and the payload is joined from them, in the
    bridge's own encoding, so what is measured here is what is sent.
    """
    parts = [encode(entry) for entry in entries]
    head, tail = '{"recordings":[', "]}"
    sizes = [len(part.encode("utf-8")) for part in parts]
    total = len(head) + len(tail) + sum(sizes) + max(0, len(parts) - 1)
    kept = len(parts)
    while kept and total > allowed:
        kept -= 1
        total -= sizes[kept] + (1 if kept else 0)
    encoded = head + ",".join(parts[:kept]) + tail
    return encoded, len(parts) - kept


def locate(path, locations, mounts=None):
    """`(location, file, id)` of a recording file, as the library will list it - or Nones.

    The path is resolved as the library resolves a location, so a recording a timer
    writes under `/hdd/movie` has the id it has under `/media/hdd/movie`. The location is
    the resolved path of the innermost one that holds it - the one whose walk lists it,
    since an outer location leaves an inner one's folder out; outside every location
    there is none.
    """
    if not path:
        return None, None, None
    mounts = read_mounts() if mounts is None else mounts
    real = resolve(os.path.normpath(path), mounts)
    best = None
    for location in locations or ():
        root = resolve(location, mounts)
        if _under(real, root) and real != root and (best is None or len(root) > len(best)):
            best = root
    if best is None:
        return None, None, recording_id(real)
    return best, shown(os.path.relpath(real, best)), recording_id(real)


# --------------------------------------------------------------- publisher --


def _run_thread(job, name):
    thread = threading.Thread(target=job, name=name, daemon=True)
    thread.start()


def _present(path):
    """Whether a file is there - asked in a worker: on a share that hangs, so does this."""
    try:
        return os.path.lexists(path)
    except OSError:
        return False


def _allow_copy():
    """`allowCopy` as each image's own move into the trash passes it.

    OpenViX and OpenATV allow a copy, OpenPLi does not: its trash move refuses to cross
    a file system, and the trash is on the recording's own anyway.
    """
    try:
        from .boxinfo import image_version

        return "openpli" not in str(image_version() or "").lower()
    except Exception:
        return True


def _merge(queued, reason, only):
    """What is read after the running read: the queued reasons and locations, joined."""
    if queued is None:
        return (reason, only)
    if queued[0] == REFRESH or reason == REFRESH:
        return (REFRESH, None)
    if queued[1] is None or only is None:
        return (reason, None)
    return (reason, set(queued[1]) | set(only))


class LibraryPublisher(Publisher):
    """`recordings` and `recordings/<folder_slug>` - the library - and its three commands."""

    name = CAPABILITY

    # Where the worker runs. A test runs it inline.
    run_in_background = staticmethod(_run_thread)
    mounts_path = MOUNTS_PATH

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._serial = 0
        self._stopped = True
        self._reading = False
        self._running_reason = None
        self._again = None
        self._walk_in_flight = False
        self._bouquets_moved = False
        self._read_reason = None
        self._read_at = None
        self._locations = []
        self._extras = set()
        # location -> {"mounted", "free_mb", "folders": [(relative, [records])], "real"}
        self._walked = {}
        # The published shape: the index, and suffix -> (payload, encoded) of each folder.
        self._index = None
        self._folders = {}
        self._by_id = {}
        self._slices = Ticker(self._next_slice, "recordings")
        self._slice_job = None
        self._settle = Ticker(self._settled, "recordings settle")
        self._settle_reason = None
        self._settle_only = None
        self._recording_in = set()
        self._watchdog = Ticker(self._read_stuck, "recordings watchdog")
        self._nav = None
        self._play_claimed = None
        self._delete_claimed = None
        self._deleting = None
        self._delete_poll = Ticker(self._check_deleted, "recording delete")
        self._space_running = False
        self._space_skipped = False
        self._late = []
        self._late_ticker = Ticker(self._say_late, "recordings refusals")
        self._channels = None

    # ------------------------------------------------------------ lifecycle --

    def start(self):
        configured = configured_locations()
        if configured is None:
            return False
        self._stopped = False
        self._set_locations(configured)
        self._bind_record_event()
        channels = self.bridge.publisher("channels") if self.bridge is not None else None
        if channels is not None:
            channels.when_changed(self._bouquets_changed)
            self._channels = channels
        if self._walk_in_flight:
            # Stopped and started again with a read still out: that read is this start's.
            self._reading = True
            self._running_reason = START
            self._watchdog.start(READ_WATCHDOG_SECONDS * 1000, True)
        else:
            self.request(START)
        return True

    def stop(self):
        self._stopped = True
        self._reading = False
        self._running_reason = None
        self._again = None
        self._slices.stop()
        self._slice_job = None
        self._settle.stop()
        self._watchdog.stop()
        self._delete_poll.stop()
        self._deleting = None
        self._late_ticker.stop()
        self._late = []
        self._unbind_record_event()
        channels, self._channels = self._channels, None
        if channels is not None:
            channels.not_when_changed(self._bouquets_changed)

    def _set_locations(self, configured):
        self._locations = [location for location, _extra in configured]
        self._extras = {location for location, extra in configured if extra}

    def claimed(self):
        return self._index is not None

    def extra_capabilities(self):
        if self._index is None:
            return []
        names = []
        if self._can_play():
            names.append(PLAY_CAPABILITY)
        if self._can_delete():
            names.append(DELETE_CAPABILITY)
        return names

    def _can_play(self):
        return player_available()

    def _can_delete(self):
        return bool(self.value(PERMISSION)) and trash_state() is True

    def _announce_if_changed(self):
        """Say `info` again when what this area claims moved: read on the main loop."""
        play, delete = self._can_play(), self._can_delete()
        changed = (play, delete) != (self._play_claimed, self._delete_claimed)
        self._play_claimed, self._delete_claimed = play, delete
        return changed

    # ------------------------------------------------------------- triggers --

    def _bind_record_event(self):
        nav = getattr(self.session, "nav", None) if self.session is not None else None
        hook = getattr(nav, "record_event", None) if nav is not None else None
        if hook is None:
            return
        try:
            hook.append(self._on_record_event)
            self._nav = nav
        except Exception as error:
            missing("session.nav.record_event", error)

    def _unbind_record_event(self):
        nav, self._nav = self._nav, None
        hook = getattr(nav, "record_event", None) if nav is not None else None
        try:
            if hook is not None and self._on_record_event in hook:
                hook.remove(self._on_record_event)
        except Exception:
            LOG.debug("could not detach from session.nav.record_event")

    def _on_record_event(self, _service=None, event=None):
        """A recording started or ended: read the locations it writes into, once it settles.

        The locations of every recording running now, and of those that ran at the
        last event - an ended recording is no longer in the timer list. When none can
        be placed, every location is read.
        """
        wanted = set()
        owner = enigma_attribute("iRecordableService")
        for name in ("evStart", "evEnd"):
            value = getattr(owner, name, None) if owner is not None else None
            if value is not None:
                wanted.add(value)
        if wanted and event not in wanted:
            return
        now = self._recording_locations()
        touched = now | self._recording_in
        self._recording_in = now
        if self._settle_reason is None:
            pending = touched or None
        elif self._settle_only is None or not touched:
            pending = None
        else:
            pending = self._settle_only | touched
        self._settle_only = pending
        self._settle_reason = RECORDING
        if not self._settle.start(RECORD_SETTLE_MILLISECONDS, True):
            self._settled()

    def _recording_locations(self):
        """The resolved locations the running recordings write into."""
        found = set()
        mounts = read_mounts(self.mounts_path)
        for base in self._recorded_paths():
            location, _file, _id = locate(base + ".ts", self.listed_locations(), mounts)
            if location is not None:
                found.add(location)
        return found

    def _settled(self):
        reason, self._settle_reason = self._settle_reason, None
        only, self._settle_only = self._settle_only, None
        if reason is not None:
            self.request(reason, only=only)

    def _bouquets_changed(self):
        """The channel list moved: `bouquet` on every entry is asked again - no disk is read."""
        if self._index is None or self._stopped:
            return
        if self._reading:
            # Asked again once the running read is done: it may have asked the channel list
            # already.
            self._bouquets_moved = True
            return
        self._bouquets_moved = False
        self._serial += 1
        self._reading = True
        self._running_reason = None
        self._watchdog.start(READ_WATCHDOG_SECONDS * 1000, True)
        # No reason: not a read of the library, so `read` and `reason` stay as they are.
        self._annotate(self._serial, self._walked, None, None)

    def request(self, reason, only=None):
        """Read the library, for `reason`. False when a read is running and answers this one.

        `only` is the resolved locations to read, the others keeping what they held; None
        reads every one. A refresh is never dropped: during a read that is not one it is
        read after it.
        """
        if self._stopped:
            return False
        if self._reading:
            if reason == REFRESH and self._running_reason == REFRESH:
                return False
            self._again = _merge(self._again, reason, only)
            return False
        configured = configured_locations()
        if configured is not None:
            self._set_locations(configured)
        locations = list(self._locations)
        extras = set(self._extras)
        previous = dict(self._walked)
        self._serial += 1
        serial = self._serial
        self._reading = True
        self._running_reason = reason
        self._walk_in_flight = True
        self._watchdog.start(READ_WATCHDOG_SECONDS * 1000, True)
        mounts_path = self.mounts_path
        selected = None if only is None else set(only)

        def job():
            try:
                walked = self._walk(locations, extras, previous, reason, mounts_path, selected)
            except Exception:
                LOG.exception("the recording library could not be read")
                walked = None
            self._to_main(self._walked_done, serial, reason, walked, time.time())

        try:
            self.run_in_background(job, "mqttbridge-recordings")
        except Exception:
            LOG.exception("could not start reading the recording library")
            self._walk_in_flight = False
            self._finish(serial)
            return False
        return True

    def _to_main(self, function, *arguments):
        client = getattr(self.bridge, "client", None) if self.bridge is not None else None
        dispatch = getattr(client, "_dispatch", None)
        if dispatch is None:
            LOG.debug("the recording library has no main-loop dispatcher; dropping a result")
            return
        try:
            dispatch(function, *arguments)
        except Exception:
            LOG.exception("could not return to the main loop")

    def _in_worker(self, question, answer):
        """Ask `question()` in a worker, and hand its answer to `answer` on the main loop."""

        def job():
            try:
                result = question()
            except Exception:
                LOG.exception("a file check raised")
                result = None
            self._to_main(answer, result)

        try:
            self.run_in_background(job, "mqttbridge-recordings-check")
        except Exception:
            LOG.exception("could not start a file check")
            answer(None)

    def _read_stuck(self):
        """The watchdog: a read that never came back - a share that hangs - is given up.

        Its answer, should it come after all, is dropped, and what was asked for during it
        is read now.
        """
        if not self._reading or self._stopped:
            return
        LOG.warning("the recording library read has not finished within %d s and is given up; "
                    "a movie location may not be answering", READ_WATCHDOG_SECONDS)
        self._serial += 1
        # The hung read is not this publisher's any more: a restart starts a read of its own.
        self._walk_in_flight = False
        self._slices.stop()
        self._slice_job = None
        self._finish(self._serial)

    @staticmethod
    def _walk(locations, extras, previous, reason, mounts_path, only=None):
        """The worker: each location walked when it is mounted - or on a refresh, whatever.

        A location that is not mounted keeps what it held when it was last read: an
        automounted share is unmounted when it has been idle, and its recordings are
        not gone. Only a refresh walks it - which mounts it. A location inside another
        is walked by itself, and the outer one leaves its folder out. The timer and the
        instant-record locations are left out where a location listed before holds them.
        """
        started = time.monotonic()
        mounts = read_mounts(mounts_path)
        kept = kept_locations(locations, extras, mounts)
        walked = {}
        woke = False
        for location, real in kept:
            mounted = is_mounted(real, mounts)
            earlier = previous.get(location) or {}
            if only is not None and real not in only and earlier:
                walked[location] = dict(earlier, real=real)
                continue
            if mounted or reason == REFRESH:
                inner = {other for _, other in kept if other != real and _under(other, real)}
                folders = walk(real, inner) if os.path.isdir(real) else []
                woke = woke or not mounted
            else:
                folders = earlier.get("folders") or []
            walked[location] = {"mounted": mounted, "folders": folders, "real": real}
        if woke:
            # A refresh that walked into an automounted share mounted it: say so now, and
            # not as a mount the minute timer would read the library again for.
            mounts = read_mounts(mounts_path)
        for location, real in kept:
            one = walked[location]
            one["mounted"] = is_mounted(real, mounts)
            one["free_mb"] = free_megabytes(real) if one["mounted"] else None
        count = sum(len(records) for one in walked.values() for _, records in one["folders"])
        LOG.info("recording library read: %d recording(s) in %d location(s), %.3fs (%s)",
                 count, len(kept), time.monotonic() - started, reason)
        return walked

    def _walked_done(self, serial, reason, walked, read_at):
        self._walk_in_flight = False
        if serial != self._serial or self._stopped:
            return
        if walked is None:
            self._finish(serial)
            return
        self._annotate(serial, walked, reason, read_at)

    # ------------------------------------------------------ the main loop's half --

    def _annotate(self, serial, walked, reason, read_at):
        """Names and bouquets from the image, in slices; then the payloads, in the worker."""
        locations = list(walked)
        srefs = []
        known = set()
        for location in locations:
            for _relative, records in walked[location]["folders"]:
                for record in records:
                    sref = record.get("sref")
                    if sref and sref not in known:
                        known.add(sref)
                        srefs.append(sref)
        names = {}
        bouquets = {}
        channels = self.bridge.publisher("channels") if self.bridge is not None else None
        pending = list(srefs)

        def names_slice():
            if serial != self._serial or self._stopped:
                return None
            deadline = time.monotonic() + SLICE_SECONDS
            done = 0
            while pending:
                if done >= NAMES_PER_SLICE or (done and time.monotonic() >= deadline):
                    break
                sref = pending.pop(0)
                done += 1
                names[sref] = service_name(sref) or None
                bouquet = None
                if channels is not None:
                    try:
                        bouquet = channels.bouquet_for(sref)
                    except Exception:
                        bouquet = None
                bouquets[sref] = bouquet
            if pending:
                return names_slice
            self._build(serial, walked, locations, names, bouquets, reason, read_at)
            return None

        self._run_slices(names_slice)

    def _run_slices(self, job):
        """Run `job` now, and what it hands back on the next turns, until it hands back None."""
        while job is not None:
            started = time.monotonic()
            job = job()
            if job is not None and time.monotonic() - started >= SLICE_SECONDS:
                self._slice_job = job
                if not self._slices.start(0, True):
                    continue
                return

    def _next_slice(self):
        job, self._slice_job = self._slice_job, None
        if job is not None and not self._stopped:
            self._run_slices(job)

    def _build(self, serial, walked, locations, names, bouquets, reason, read_at):
        if self.bridge is None:
            self._finish(serial)
            return
        folder_keys = []
        for location in locations:
            for relative, _records in walked[location]["folders"]:
                folder_keys.append((walked[location]["real"], relative))
        from .bridge import MAX_PACKET_BYTES, PACKET_OVERHEAD_BYTES

        slugs = folder_slugs(folder_keys)
        allowed = {
            slug: MAX_PACKET_BYTES - PACKET_OVERHEAD_BYTES
            - len(self.bridge.topic(FOLDER_PREFIX + slug).encode("utf-8"))
            for slug in slugs
        }
        encode = self.bridge.encode

        def job():
            try:
                built = self._payloads(walked, locations, slugs, allowed, names, bouquets,
                                       encode)
            except Exception:
                LOG.exception("the recording library could not be built")
                built = None
            self._to_main(self._publish_built, serial, walked, reason, read_at, built)

        try:
            self.run_in_background(job, "mqttbridge-recordings")
        except Exception:
            LOG.exception("could not start building the recording library")
            self._finish(serial)

    @staticmethod
    def _payloads(walked, locations, slugs, allowed, names, bouquets, encode):
        """The worker: every folder's payload, newest first and cut to the bound, encoded."""
        index_locations = []
        folders = {}
        by_id = {}
        position = 0
        for location in locations:
            one = walked[location]
            folder_rows = []
            for relative, records in one["folders"]:
                slug = slugs[position]
                position += 1
                entries = []
                for record in newest_first(records):
                    sref = record.get("sref")
                    entry = {
                        "id": record["id"],
                        "file": record["file"],
                        "name": record["name"],
                        "description": record["description"],
                        "service_name": (names.get(sref) if sref else None)
                        or record.get("_channel"),
                        "sref": sref,
                        "bouquet": bouquets.get(sref) if sref else None,
                        "begin": record["begin"],
                        "duration": record["duration"],
                        "size_mb": record["size_mb"],
                    }
                    entries.append(entry)
                    by_id[record["id"]] = {
                        "path": record["_path"],
                        "location": one["real"],
                        "file": record["file"],
                        "name": record["name"],
                    }
                encoded, cut = fit(entries, encode, allowed[slug])
                kept = entries[: len(entries) - cut]
                for entry in entries[len(kept):]:
                    by_id.pop(entry["id"], None)
                folders[FOLDER_PREFIX + slug] = ({"recordings": kept}, encoded)
                folder_rows.append({
                    "slug": slug,
                    "path": relative,
                    "count": len(kept),
                    "size_mb": sum(entry["size_mb"] for entry in kept),
                    "cut": cut,
                })
            index_locations.append({
                "path": one["real"],
                "mounted": bool(one["mounted"]),
                "free_mb": one["free_mb"],
                "folders": folder_rows,
            })
        return index_locations, folders, by_id

    def _publish_built(self, serial, walked, reason, read_at, built):
        if serial != self._serial or self._stopped:
            return
        if built is None:
            self._finish(serial)
            return
        index_locations, folders, by_id = built
        first = self._index is None
        self._walked = walked
        if reason is not None:
            self._read_reason = reason
            # Strictly later than the read before, also within one second: a consumer
            # tells a new read from a republished index by `read` alone.
            previous = self._read_at
            self._read_at = int(read_at) if previous is None else max(int(read_at),
                                                                     previous + 1)
        self._index = {"read": self._read_at, "reason": self._read_reason,
                       "locations": index_locations}
        self._folders = folders
        self._by_id = by_id
        pending = sorted(folders)

        def publish_slice():
            if serial != self._serial or self._stopped:
                return None
            deadline = time.monotonic() + SLICE_SECONDS
            while pending:
                suffix = pending.pop(0)
                payload, encoded = self._folders.get(suffix, (None, None))
                if payload is not None:
                    self.publish(suffix, payload, encoded)
                if time.monotonic() >= deadline:
                    break
            if pending:
                return publish_slice
            self.publish(INDEX, self._index)
            self.retract_stale()
            changed = self._announce_if_changed()
            if first:
                LOG.info("the recording library is read; recordings is available")
            if (first or changed) and self.bridge is not None:
                self.bridge.announce_capabilities()
            self._finish(serial)
            return None

        self._run_slices(publish_slice)

    def _finish(self, serial):
        if serial != self._serial:
            return
        self._watchdog.stop()
        self._reading = False
        self._running_reason = None
        again, self._again = self._again, None
        if self._stopped:
            return
        if again is not None:
            # The next read asks the channel list itself.
            self._bouquets_moved = False
            self.request(again[0], only=again[1])
        elif self._bouquets_moved:
            self._bouquets_changed()

    # --------------------------------------------------------- the published --

    def prepared(self, suffix, payload):
        if suffix.startswith(FOLDER_PREFIX):
            held = self._folders.get(suffix)
            if held is not None and held[0] is payload:
                return held[1], held[1]
        return None, None

    def snapshot(self):
        if self._index is None:
            return {}
        topics = {suffix: held[0] for suffix, held in sorted(self._folders.items())}
        topics[INDEX] = self._index
        return topics

    def published_suffixes(self):
        return set(self._folders) | ({INDEX} if self._index is not None else set())

    def retract_stale(self):
        """Retract every folder topic this node has retained and the index no longer lists.

        From the state file, so a folder that went while the plugin was not running is
        found as well. Only on an open session, as for the channel lists: a retraction
        sent with no session reaches nobody, and forgetting the topic then would leave
        it on the broker with nothing that knows of it.
        """
        bridge = self.bridge
        if bridge is None or not bridge.connected or self._index is None:
            # Before the first read nothing is known to be stale: the folders an earlier
            # run left are this run's too until the read says otherwise.
            return 0
        prefix = bridge.topic(FOLDER_PREFIX)
        current = {bridge.topic(suffix) for suffix in self._folders}
        stale = [
            topic for topic in bridge.state.retained_topics
            if topic.startswith(prefix) and topic not in current
        ]
        for topic in stale:
            LOG.info("retracting a recording folder the library no longer lists")
            bridge.retract(topic)
        if stale:
            bridge.state.save()
        return len(stale)

    # ---------------------------------------------- free space, from hdd.py --

    @property
    def locations(self):
        """The configured movie locations, as the settings name them."""
        return list(self._locations)

    def listed_locations(self):
        """The resolved locations the last read listed, in its order.

        What `locate` places a recording in, so that a running recording's location and
        file are the ones the index and its folder topic give it - a timer or an
        instant-record location that another location holds is not one of them. Before
        the first read, the configured ones as a read would keep them: resolved, without
        repeats, and without a timer or instant-record location another one holds - from
        `/proc/mounts` and the links on the flash, without a disk access.
        """
        listed = [one.get("real") for one in self._walked.values() if one.get("real")]
        if listed:
            return listed
        mounts = read_mounts(self.mounts_path)
        return [real for _location, real in kept_locations(self._locations, self._extras, mounts)]

    def check_space(self):
        """The recording disk's minute: mounts and free space, in a thread of their own.

        Never in the recording disk's own thread, so a share that hangs in `statvfs`
        cannot hold up the `hdd` topic; and never two at once, so it cannot pile up
        threads either - while one hangs, the minute is skipped.
        """
        if self._stopped or self._index is None:
            return False
        if self._space_running:
            if not self._space_skipped:
                self._space_skipped = True
                LOG.warning("the movie locations' free space is still being read; a movie "
                            "location may not be answering")
            return False
        self._space_running = True
        locations = list(self._locations)
        mounts_path = self.mounts_path

        def job():
            try:
                result = space(locations, mounts_path)
            except Exception:
                LOG.exception("the movie locations could not be measured")
                result = None
            self._to_main(self._space_done, result)

        try:
            self.run_in_background(job, "mqttbridge-recordings-space")
        except Exception:
            self._space_running = False
            LOG.exception("could not start measuring the movie locations")
            return False
        return True

    def _space_done(self, result):
        self._space_running = False
        self._space_skipped = False
        if result:
            self.on_space(result)

    def on_space(self, result):
        """The minute's `/proc/mounts` and `statvfs`, back on the main loop.

        A location that is mounted now and was not is read again (`mount`). One that
        went keeps its recordings, with `mounted` false. A change of free space goes
        out on the index alone, `read` and `reason` as they were.
        """
        if self._stopped or self._index is None or not result:
            return
        newly = set()
        changed = False
        rows = {row["path"]: row for row in self._index["locations"]}
        for location, one in self._walked.items():
            known = result.get(location)
            row = rows.get(one.get("real"))
            if known is None or row is None:
                continue
            mounted, free = known
            if mounted and not row["mounted"]:
                newly.add(one.get("real"))
            if (bool(mounted), free) != (row["mounted"], row["free_mb"]):
                row["mounted"] = bool(mounted)
                row["free_mb"] = free
                one["mounted"] = bool(mounted)
                one["free_mb"] = free
                changed = True
        if changed:
            self.publish(INDEX, self._index)
        if newly:
            self.request(MOUNT, only=newly)

    # -------------------------------------------------------------- lookups --

    def find(self, recording_id):
        """`{"path", "location", "file", "name"}` of a recording the library lists, or None."""
        return self._by_id.get(recording_id)

    def _recorded_paths(self):
        nav = getattr(self.session, "nav", None) if self.session is not None else None
        timer = getattr(nav, "RecordTimer", None) if nav is not None else None
        paths = set()
        for entry in list(getattr(timer, "timer_list", None) or []):
            try:
                checker = getattr(entry, "isRunning", None)
                running = bool(checker()) if callable(checker) else False
            except Exception:
                running = False
            if not running or getattr(entry, "justplay", False):
                continue
            base = getattr(entry, "Filename", None)
            if base:
                paths.add(os.path.normpath(str(base)))
        return paths

    def _being_recorded(self, path):
        """Whether a running recording timer writes this file - its `Filename` has no extension."""
        mounts = read_mounts(self.mounts_path)
        base = path[: path.rfind(".")] if "." in os.path.basename(path) else path
        wanted = resolve(os.path.normpath(base), mounts)
        return any(resolve(one, mounts) == wanted for one in self._recorded_paths())

    def _being_played(self, path):
        """Whether the file playing is this one - by the reference's decoded path."""
        from .enigma2 import navigation

        nav = navigation(self.session)
        getter = getattr(nav, "getCurrentlyPlayingServiceReference", None) if nav else None
        try:
            playing = file_of(getter()) if callable(getter) else ""
        except Exception:
            playing = ""
        if not playing or not playing.startswith("/"):
            return False
        mounts = read_mounts(self.mounts_path)
        return resolve(os.path.normpath(playing), mounts) == resolve(os.path.normpath(path),
                                                                     mounts)

    # ------------------------------------------------------------- refusals --

    def _report_later(self, command, message, command_id):
        """A refusal of work a command left for later: on the next turn, after the command.

        The dispatcher clears `last_error` when the command it ran returned, so a refusal
        found while it ran would be taken back at once; and found in a worker it is late
        anyway.
        """
        self._late.append((command, message, command_id))
        if not self._late_ticker.start(0, True):
            self._say_late()

    def _say_late(self):
        late, self._late = self._late, []
        for command, message, command_id in late:
            self.report(command, message, command_id)

    # ------------------------------------------------------------- commands --

    def refresh_command(self):
        """`cmd/recordings_refresh`: read now. A refresh already running answers it."""
        self.request(REFRESH)
        return None

    def delete(self, recording_id, origin=MQTT, command_id=None):
        """`cmd/delete_recording`: into the image's trash, and nowhere else."""
        if not granted(self.value, PERMISSION, origin):
            return refusal(NOT_PERMITTED)
        trash = trash_state()
        if trash is None:
            return refusal(NO_TRASH)
        if trash is False:
            return refusal(TRASH_OFF)
        found = self.find(recording_id)
        if found is None:
            return refusal(UNKNOWN)
        path = found["path"]
        if self._being_recorded(path):
            return refusal(RECORDED)
        if self._being_played(path):
            return refusal(PLAYED)
        if self._deleting is not None:
            return refusal(IN_PROGRESS)
        if _create_trash() is None or _move_service_files() is None:
            return refusal(NO_TRASH)
        self._deleting = {"path": path, "found": found, "polls": 0, "id": command_id,
                          "checking": True}
        self._in_worker(lambda: _present(path), self._delete_checked)
        return None

    def _delete_checked(self, present):
        """The file is there - asked off the main loop: the image's own move into its trash."""
        deleting = self._deleting
        if deleting is None or self._stopped:
            return
        deleting["checking"] = False
        command_id = deleting["id"]
        if present is not True:
            self._deleting = None
            self._report_later("delete_recording", refusal(GONE), command_id)
            return
        path = deleting["path"]
        reference = playable_reference(path)
        create = _create_trash()
        move = _move_service_files()
        if reference is None or create is None or move is None:
            self._deleting = None
            self._report_later("delete_recording", refusal(NO_TRASH), command_id)
            return
        try:
            folder = create(path)
        except Exception:
            LOG.exception("the image's trash folder could not be made")
            folder = None
        if not folder:
            self._deleting = None
            self._report_later("delete_recording", refusal(MOVE_FAILED), command_id)
            return
        try:
            move(reference, folder, deleting["found"].get("name"), allowCopy=_allow_copy())
        except Exception as error:
            LOG.warning("the image did not move a recording to its trash: %s",
                        type(error).__name__)
            self._deleting = None
            self._report_later("delete_recording", refusal(MOVE_FAILED), command_id)
            return
        _forget_resume_point(reference)
        LOG.info("a recording is being moved to the receiver's trash")
        if not self._delete_poll.start(DELETE_POLL_MILLISECONDS):
            # No timer to watch the move with: what the library finds is the answer.
            self._deleting = None
            self.request(DELETE, only={deleting["found"]["location"]})
            return
        self._check_deleted()

    def _check_deleted(self):
        """Verified by effect: the recording's file is gone from where it was - asked in a worker.

        Counted in turns of the poll rather than by the clock, so a receiver whose
        clock is set while it waits neither gives up early nor waits for ever.
        """
        deleting = self._deleting
        if deleting is None:
            self._delete_poll.stop()
            return
        if deleting["checking"]:
            return
        deleting["checking"] = True
        path = deleting["path"]
        self._in_worker(lambda: _present(path), self._delete_polled)

    def _delete_polled(self, present):
        deleting = self._deleting
        if deleting is None or self._stopped:
            return
        deleting["checking"] = False
        location = deleting["found"]["location"]
        if present is False:
            self._deleting = None
            self._delete_poll.stop()
            self.request(DELETE, only={location})
            return
        deleting["polls"] += 1
        if deleting["polls"] * DELETE_POLL_MILLISECONDS >= DELETE_WAIT_SECONDS * 1000:
            self._deleting = None
            self._delete_poll.stop()
            self._report_later("delete_recording", refusal(NOT_MOVED), deleting["id"])
            self.request(DELETE, only={location})

    def play(self, recording_id, on_play=None, command_id=None):
        """`cmd/play_recording`, once the receiver is awake: the image's own movie player.

        The file is looked for in a worker; then, as the image's movie list does, the
        image's own timeshift question is asked when a buffer runs, and the player opens
        when the viewer leaves timeshift. What goes wrong after the command returned is
        said on `last_error` then.
        """
        found = self.find(recording_id)
        if found is None:
            return refusal(UNKNOWN)
        path = found["path"]
        reference = playable_reference(path)
        if reference is None or _infobar() is None:
            return refusal(NO_PLAYER)

        def checked(present):
            if self._stopped:
                return
            if not present:
                self._report_later("play_recording", refusal(GONE), command_id)
                return
            self._ask_timeshift(reference, on_play, command_id)

        self._in_worker(lambda: _present(path), checked)
        return None

    def _ask_timeshift(self, reference, on_play, command_id):
        infobar = _infobar()
        check = getattr(infobar, "checkTimeshiftRunning", None) if infobar is not None else None

        def answered(answer=True, *_rest):
            if not answer:
                self._report_later("play_recording", refusal(TIMESHIFT_KEPT), command_id)
                return
            error = self._open_player(reference, on_play)
            if error:
                self._report_later("play_recording", error, command_id)

        if not callable(check):
            answered(True)
            return
        try:
            check(answered)
        except Exception:
            LOG.exception("the image's timeshift question could not be asked")
            answered(True)

    def _open_player(self, reference, on_play):
        infobar = _infobar()
        if infobar is None:
            return refusal(NO_PLAYER)
        player_class = _movie_player_class()
        dialog = getattr(self.session, "current_dialog", None)
        try:
            if player_class is not None and isinstance(dialog, player_class) and callable(
                getattr(dialog, "movieSelected", None)
            ):
                # A recording is playing already: the image's player plays the next one itself.
                dialog.movieSelected(reference)
            elif callable(getattr(infobar, "openMoviePlayer", None)):
                if self._parental_allows(reference, infobar.openMoviePlayer):
                    infobar.openMoviePlayer(reference)
                else:
                    # The image asks for its PIN on the television and opens the player itself.
                    return None
            elif callable(getattr(infobar, "movieSelected", None)):
                nav = getattr(self.session, "nav", None)
                getter = getattr(nav, "getCurrentlyPlayingServiceOrGroup", None) or getattr(
                    nav, "getCurrentlyPlayingServiceReference", None)
                infobar.lastservice = getter() if getter is not None else None
                infobar.movieSelected(reference)
            else:
                return refusal(NO_PLAYER)
        except Exception as error:
            LOG.exception("the image's movie player could not be opened")
            return "the receiver could not play that recording: " + type(error).__name__
        if on_play is not None:
            on_play(reference_string(reference))
        return None

    @staticmethod
    def _parental_allows(reference, callback):
        try:
            from Components.ParentalControl import parentalControl
        except Exception:
            return True
        check = getattr(parentalControl, "isServicePlayable", None)
        if check is None:
            return True
        try:
            return bool(check(reference, callback))
        except Exception:
            return True


def payload_id(text):
    """The id out of a command's payload, or None when the payload is not exactly `{"id": ...}`."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"id"}:
        return None
    value = payload["id"]
    return value if valid_id(value) else None
