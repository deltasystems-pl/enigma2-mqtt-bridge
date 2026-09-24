"""Wake-on-LAN: what the image says it can do, and the image's own switch.

**The image decides, not the network card.** `ethtool` will report a Vu+ Uno 4K
SE's Ethernet MAC as `Supports Wake-on: gs`, and it is telling the truth about
the wrong thing: that is the MAC's ability to wake the system from a Linux
*suspend*, programmed only by the driver's suspend callback. An enigma2 image
never suspends. Its deep standby takes the interface down, hands the box to the
front processor and powers off, so a flag set with `ethtool -s ... wol g` is stored
and then read by nothing - and a plugin that set it, read it back and published
„armed" would be publishing a receiver that cannot be woken as one that can
(ADR-0012).

What an image does have, where the hardware allows it, is a front-processor
switch. `Components.SystemInfo` probes for it at start and records its path as
`SystemInfo["WakeOnLAN"]` - `/proc/stb/fp/wol`, or `/proc/stb/power/wol` on the
two machines that have that one - or `False` when the driver created neither.
Only when it is there does the image build `config.usage.wakeOnLAN` („Wake On
LAN" in its expert settings), whose notifier writes the file at every start and
on every change. So this module does exactly two things, and both go through
what the image already built:

- `report()` is `info.wol`, read from the image at every `info` publish and
  never inferred from anything this plugin did.
- `arm()` switches the image's own setting on, when `wol_arm` says so and the
  setting exists. It never switches it off: somebody may have switched it on in
  the image's menu, and this plugin arms, it does not disarm.

🔴 Nothing here starts a process - no `ethtool`, no shell, no
`eConsoleAppContainer`. There is nothing on this path a command line could do
that the image's own setting does not, and a test asserts it stays that way.
"""

import os

from . import boxinfo
from . import config as settings_module
from .log import get_logger

LOG = get_logger("wol")

# The two files the image knows, by the directory that names the vocabulary
# its notifier writes into them: `enable`/`disable` under `fp`, `on`/`off`
# under `power`.
MECHANISM_FP = "fp"
MECHANISM_POWER = "power"

# Both vocabularies are accepted whichever file they came from: what the
# driver echoes back is not measured on any receiver this project has seen, and
# a word that means „on" in one file does not mean „off" in the other.
_ARMED_WORDS = ("enable", "on")
_DISARMED_WORDS = ("disable", "off")


def _system_info():
    """The image's `SystemInfo`, or None where the image has none."""
    try:
        from Components.SystemInfo import SystemInfo

        return SystemInfo
    except Exception:
        return None


def switch_path():
    """The image's Wake-on-LAN file; "" when the image says it has none; None when unknown.

    🔴 Three answers, not two. `False` in `SystemInfo["WakeOnLAN"]` is the
    image's own statement that its probe found no switch, and only that becomes
    "" - „this receiver cannot be woken over the network". Everything else that
    is not a path - no `SystemInfo` to import, a `get` that raises, a key the
    image never set, a value of a type its probe never produces - is a question
    that went unanswered, and a consumer that read it as „no switch" would tell
    a household something nobody measured.
    """
    info = _system_info()
    if info is None:
        return None
    try:
        found = info.get("WakeOnLAN")
    except Exception:
        LOG.debug("SystemInfo could not be asked for WakeOnLAN")
        return None
    if found is False or found == "":
        return ""
    if isinstance(found, str):
        return found
    return None


def supported():
    """True only when the image named its switch. Unknown is not a switch to act on."""
    return bool(switch_path())


def mechanism(path):
    """`fp` or `power` for the image's file, None when there is none.

    Named by the directory the file sits in. A path under neither - an image
    that moved it - is read the way the image's own notifier reads it: `fp`
    anywhere in the path means the `fp` vocabulary.
    """
    if not path:
        return None
    folder = os.path.basename(os.path.dirname(path))
    if folder in (MECHANISM_FP, MECHANISM_POWER):
        return folder
    return MECHANISM_FP if MECHANISM_FP in path else MECHANISM_POWER


def _image_setting():
    """`config.usage.wakeOnLAN`, or None on an image - or a box - that did not build it."""
    try:
        from Components.config import config

        usage = getattr(config, "usage", None)
        return getattr(usage, "wakeOnLAN", None) if usage is not None else None
    except Exception:
        return None


def _read_switch(path):
    """What the file says: True, False, or None when it cannot be read or says neither."""
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            word = handle.read(64).strip().lower()
    except OSError:
        return None
    if word in _ARMED_WORDS:
        return True
    if word in _DISARMED_WORDS:
        return False
    return None


def armed(path):
    """Whether the image's switch is on, as the image states it; None when not supported.

    The file first, because it is what the front processor is told. Where it
    cannot be read - the driver may make it write-only, which is unmeasured -
    the image's own setting answers, since its notifier is what writes the
    file. Never this plugin's `wol_arm`: that is what somebody asked for, not
    what the receiver is.
    """
    if not path:
        return None
    state = _read_switch(path)
    if state is not None:
        return state
    element = _image_setting()
    if element is None:
        return None
    try:
        value = element.value
    except Exception:
        return None
    return value if isinstance(value, bool) else None


def report():
    """`info.wol`: every key, always, and None for what could not be read.

    `supported` is `false` only when the image said so; a failure anywhere on
    the way is `null`, because `false` is what a consumer turns into „this
    receiver cannot be woken". The interface is resolved on its own, so a
    failure reading the image does not also cost the part that was read.
    """
    try:
        iface = boxinfo.mac_interface() or None
    except Exception:
        LOG.exception("the Wake-on-LAN interface could not be resolved")
        iface = None
    try:
        path = switch_path()
        return {
            "supported": None if path is None else bool(path),
            "armed": armed(path),
            "iface": iface,
            "mechanism": mechanism(path),
        }
    except Exception:
        LOG.exception("Wake-on-LAN could not be read")
        return {"supported": None, "armed": None, "iface": iface, "mechanism": None}


def arm(section=None):
    """Switch the image's own Wake-on-LAN on when `wol_arm` asks for it. Never raises.

    Returns True only when this call changed the image's setting. Setting the
    value is what makes the image's notifier write the front-processor file;
    saving it is what makes the image write it again at the next start. A
    setting that is already on is left alone, so a start does not rewrite
    enigma2's settings file for nothing.
    """
    try:
        if not settings_module.value("wol_arm", section):
            return False
        if not supported():
            return False
        element = _image_setting()
        if element is None:
            LOG.warning("the image reports Wake-on-LAN but built no setting for it")
            return False
        if element.value is True:
            return False
        element.value = True
        element.save()
        from Components.config import configfile

        configfile.save()
        LOG.info("switched on the image's Wake-on-LAN setting")
        return True
    except Exception:
        LOG.exception("the image's Wake-on-LAN setting could not be switched on")
        return False
