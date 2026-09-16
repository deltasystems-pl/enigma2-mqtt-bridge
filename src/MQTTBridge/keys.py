"""Remote-key names, both ways.

The table below is the Linux input event name every enigma2 image uses, which is
what the `key` topic carries and what `cmd/key` accepts.

The receiver has its own copy of this mapping in `keyids`, with a few hundred
entries to this file's forty, and it is merged in at first use — so a box with
a remote this plugin has never heard of still publishes `KEY_PVR` rather than
`KEY_393`. This file is not thereby redundant: it is what the contract means by
a key name off a receiver, it is what the tests run against, and it decides
which name wins when the image lists several for one code.
"""

# enigma2 exposes the Linux input codes; these are the names the contract uses.
KEY_NAMES = {
    352: "KEY_OK",
    103: "KEY_UP",
    108: "KEY_DOWN",
    105: "KEY_LEFT",
    106: "KEY_RIGHT",
    174: "KEY_EXIT",
    139: "KEY_MENU",
    358: "KEY_INFO",
    116: "KEY_POWER",
    398: "KEY_RED",
    399: "KEY_GREEN",
    400: "KEY_YELLOW",
    401: "KEY_BLUE",
    402: "KEY_CHANNELUP",
    403: "KEY_CHANNELDOWN",
    115: "KEY_VOLUMEUP",
    114: "KEY_VOLUMEDOWN",
    113: "KEY_MUTE",
    167: "KEY_RECORD",
    164: "KEY_PLAYPAUSE",
    128: "KEY_STOP",
    168: "KEY_REWIND",
    208: "KEY_FASTFORWARD",
    365: "KEY_EPG",
    362: "KEY_PROGRAM",
    377: "KEY_TV",
    385: "KEY_RADIO",
    388: "KEY_TEXT",
    375: "KEY_SCREEN",
    2: "KEY_1",
    3: "KEY_2",
    4: "KEY_3",
    5: "KEY_4",
    6: "KEY_5",
    7: "KEY_6",
    8: "KEY_7",
    9: "KEY_8",
    10: "KEY_9",
    11: "KEY_0",
}

KEY_CODES = {name: code for code, name in KEY_NAMES.items()}

# The four the contract turns into Home Assistant device triggers.
COLOUR_KEYS = ("KEY_RED", "KEY_GREEN", "KEY_YELLOW", "KEY_BLUE")

PRESS_SHORT = "short"
PRESS_LONG = "long"

_names = None
_codes = None


def _load():
    """Merge the receiver's own key table into this one, once.

    `keyids.KEYIDS` maps a name to a code, and more than one name can share a
    code — `KEY_OK` and `KEY_ENTER` are the same button on some remotes. The
    table above wins wherever it has an opinion, so the name on the `key` topic
    does not depend on the order a dictionary happened to be built in, and the
    image fills in everything else.
    """
    global _names, _codes
    if _names is not None:
        return
    names = dict(KEY_NAMES)
    codes = dict(KEY_CODES)
    try:
        from keyids import KEYIDS
    except Exception:
        KEYIDS = {}
    for name, code in sorted((KEYIDS or {}).items()):
        try:
            code = int(code)
        except (TypeError, ValueError):
            continue
        name = str(name)
        codes.setdefault(name, code)
        names.setdefault(code, name)
    _names, _codes = names, codes


def forget_image_keys():
    """Test seam: the merged table is module state and outlives a test."""
    global _names, _codes
    _names = _codes = None


def name_for(code):
    _load()
    return _names.get(code)


def code_for(name):
    _load()
    return _codes.get(str(name or "").strip().upper())
