"""Remote-key names, both ways.

Not bound in this release. Observing the remote means an `eActionMap` binding on
the main thread, and a key handler that returns anything but `0` swallows the
button for the person holding the remote — so it arrives with the milestone that
can test it against a real remote rather than as a convenience added early.

The table below is the Linux input event name every enigma2 image uses, which is
what the `key` topic carries and what `cmd/key` accepts. It is here now so that
the contract's key names have exactly one definition.
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


def name_for(code):
    return KEY_NAMES.get(code)


def code_for(name):
    return KEY_CODES.get(str(name or "").strip().upper())
