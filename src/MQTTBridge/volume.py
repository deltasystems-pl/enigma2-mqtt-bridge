"""Volume and mute.

There is no single place a volume change passes through. The remote goes through
`VolumeControl`, OpenWebif's `/api/vol` goes through the same instance, and a
plugin that sets the hardware directly goes through neither. So this module does
both things: it wraps the four methods on `VolumeControl.instance`, which catches
every change the moment it happens, and it re-reads `eDVBVolumecontrol` every few
seconds, which catches everything else within one tick.

The reconciliation publishes **only on change**, so the second mechanism costs
one comparison per tick and nothing on the broker.

Setting the volume goes through `VolumeControl` too, and deliberately: it is what
draws the volume bar on the television. A household that cannot see the volume it
is changing will report the plugin as broken, and they will be right.
"""

from .enigma2 import Ticker, missing
from .log import get_logger
from .publisher import Publisher

LOG = get_logger("volume")

RECONCILE_MILLISECONDS = 5000

MINIMUM = 0
MAXIMUM = 100

# The methods worth watching: everything that changes volume or mute arrives
# through one of them.
WRAPPED = ("setVolume", "volUp", "volDown", "volMute")

# Set on a wrapper so a second start cannot wrap the wrappers.
MARKER = "_mqttbridge_wrapped"


def _control():
    """enigma2's own volume control, the one the remote uses."""
    try:
        from Components.VolumeControl import VolumeControl
    except Exception as error:
        missing("Components.VolumeControl", error)
        return None
    instance = getattr(VolumeControl, "instance", None)
    if instance is None:
        missing("VolumeControl.instance")
    return instance


def _hardware():
    """The volume as the hardware has it — the authority both paths end at."""
    try:
        from enigma import eDVBVolumecontrol
    except Exception as error:
        missing("eDVBVolumecontrol", error)
        return None
    try:
        return eDVBVolumecontrol.getInstance()
    except Exception:
        LOG.exception("eDVBVolumecontrol.getInstance() raised")
        return None


def read():
    """The `volume` payload, or None when this image will not say."""
    hardware = _hardware()
    if hardware is None:
        return None
    try:
        level = int(hardware.getVolume())
        muted = bool(hardware.isMuted())
    except Exception:
        LOG.exception("could not read the volume")
        return None
    return {"level": level, "muted": muted}


def clamp(level):
    return max(MINIMUM, min(MAXIMUM, int(level)))


def _cosmetic(control, attribute, method=None, *args):
    """One step of the on-screen volume bar. Never worth failing a command over."""
    target = getattr(control, attribute, None) if control is not None else None
    if target is None:
        return
    if method is not None:
        target = getattr(target, method, None)
        if target is None:
            return
    try:
        target(*args)
    except Exception:
        LOG.debug("the volume bar's %s step did nothing", attribute)


def set_level(level):
    """Set the volume, and show the bar while doing it. None on success.

    The bar is the reason this is five calls rather than one. `volUp` and
    `volDown` draw it themselves; setting an absolute value does not, so the
    dialogue is opened, given the number, and put on its own three-second timer
    — which is precisely what the receiver's own web interface does, and what a
    household expects to see when the volume changes.
    """
    wanted = clamp(level)
    control = _control()

    _cosmetic(control, "volumeDialog", "show")

    hardware_control = getattr(control, "volctrl", None) if control is not None else None
    if hardware_control is None:
        hardware_control = _hardware()
    if hardware_control is None:
        return "this image will not let the volume be set"
    try:
        # Both channels: enigma2 keeps left and right separately and every user
        # interface on the box only ever shows one number.
        hardware_control.setVolume(wanted, wanted)
    except Exception as error:
        LOG.exception("could not set the volume")
        return type(error).__name__ + ": " + str(error)

    # Without this the new volume is lost on the next restart, because enigma2
    # restores the value it last saved rather than the value the chip holds.
    _cosmetic(control, "volSave")
    _cosmetic(control, "volumeDialog", "setValue", wanted)
    _cosmetic(control, "hideVolTimer", "start", 3000, True)
    return None


def set_muted(muted):
    """Mute or unmute — and only when that is not already the state.

    `volMute` is a toggle, so calling it to reach a state it is already in is
    how a command to mute unmutes somebody's television.

    🔴 It is also a toggle that **declines to act at volume zero** unless it is
    forced, so „it did not raise" is not „it worked". The state is read back.
    """
    current = read()
    if current is None:
        return "this image will not report the volume"
    if bool(current["muted"]) == bool(muted):
        return None
    control = _control()
    if control is None:
        return "this image has no VolumeControl instance"
    toggle = getattr(control, "volMute", None)
    if toggle is None:
        return "this image's VolumeControl has no volMute"
    try:
        toggle()
    except Exception as error:
        LOG.exception("could not toggle mute")
        return type(error).__name__ + ": " + str(error)
    after = read()
    if after is not None and bool(after["muted"]) != bool(muted):
        return "the receiver would not " + ("mute" if muted else "unmute") + " (the volume is 0)"
    return None


class VolumePublisher(Publisher):
    """`volume` — {level, muted}, from whoever changed it."""

    name = "volume"

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._ticker = Ticker(self._reconcile, "volume")
        self._deferred_wrap = Ticker(self._wrap, "volume binding")
        self._originals = {}
        self._control = None

    def start(self):
        if read() is None:
            return False
        if not self._wrap():
            # WHERE_SESSIONSTART runs from Session.__init__, before OpenViX
            # constructs VolumeControl. One turn of the main loop is late enough
            # to see it, without turning a missing singleton into a retry loop.
            self._deferred_wrap.start(0, True)
        self._ticker.start(RECONCILE_MILLISECONDS)
        return True

    def stop(self):
        self._deferred_wrap.stop()
        self._ticker.stop()
        self._unwrap()

    # -------------------------------------------------------------- wrapping --

    def _wrap(self):
        if self._control is not None:
            return True
        control = _control()
        if control is None:
            # The reconciliation alone still publishes every change within five
            # seconds, so this is a slower feature area, not a missing one.
            LOG.info("no VolumeControl instance; volume changes arrive on the 5 s tick")
            return False
        self._control = control
        for name in WRAPPED:
            original = getattr(control, name, None)
            if original is None or getattr(original, MARKER, False):
                continue
            wrapper = self._make_wrapper(original)
            try:
                setattr(control, name, wrapper)
            except Exception:
                LOG.exception("could not wrap VolumeControl.%s", name)
                continue
            self._originals[name] = original
        return True

    def _make_wrapper(self, original):
        publisher = self

        def wrapper(*args, **kwargs):
            # enigma2's call first, always: this plugin is an observer, and a
            # failure to publish must never be a failure to change the volume.
            result = original(*args, **kwargs)
            try:
                publisher._publish_now()
            except Exception:
                LOG.exception("publishing a volume change raised")
            return result

        setattr(wrapper, MARKER, True)
        return wrapper

    def _unwrap(self):
        control, self._control = self._control, None
        originals, self._originals = self._originals, {}
        if control is None:
            return
        for name, original in originals.items():
            try:
                # Only put it back if it is still ours; something else may have
                # wrapped it after us, and unwinding that would be worse.
                if getattr(getattr(control, name, None), MARKER, False):
                    setattr(control, name, original)
            except Exception:
                LOG.exception("could not restore VolumeControl.%s", name)

    # ------------------------------------------------------------- publishing --

    def _publish_now(self):
        payload = read()
        if payload is not None:
            self.publish("volume", payload)

    def _reconcile(self):
        self._publish_now()

    def snapshot(self):
        payload = read()
        return {} if payload is None else {"volume": payload}
